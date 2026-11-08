"""One phone call: Twilio Media Stream <-> OpenAI Realtime, plus the call lifecycle.

Audio is base64 mu-law end to end (no transcoding). All time logic runs in ``_ticker`` against an
injectable clock so tests can drive max duration and end-of-call grace without sleeping.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from collections import deque
from collections.abc import Callable
from typing import Any

from .. import brief as briefmod
from .. import consent, states, webphone
from ..store import Store
from . import realtime as rt
from . import twilio_io
from .settings import BridgeSettings

log = logging.getLogger(__name__)

MAX_Q_LEN = 500


class CallSession:
    def __init__(self, *, ws: Any, call: dict, store: Store, settings: BridgeSettings,
                 connect: rt.RealtimeConnect, twilio_client_factory: Callable[[], Any],
                 clock: Callable[[], float], disclosed: bool = False) -> None:
        self.ws = ws  # object with receive_text() / send_text()
        self.call = call
        self.call_id: str = call["id"]
        self.store = store
        self.settings = settings
        self.connect = connect
        self._client_factory = twilio_client_factory
        self.clock = clock
        self.conn: rt.RealtimeConnection | None = None
        self.stream_sid = ""
        self.call_sid = call.get("provider_sid") or ""
        self.web = webphone.is_web_sid(self.call_sid)  # callee is a browser page, not Twilio
        self.started_at = clock()
        self.max_duration_s = int(call["brief"]["limits"]["max_duration_s"])
        self.payload = briefmod.voice_payload(call, store.consents(self.call_id))
        if disclosed:  # the carrier already played the disclosure: the model must not repeat it
            self.payload = {**self.payload, "opening_line": ""}
        # playback tracking (barge-in)
        self.latest_media_ts = 0
        self.item_id = ""
        self.item_start_ts = 0
        self.item_sent_ms = 0.0
        self.marks: set[str] = set()
        self._mark_n = 0
        self.response_active = False
        self._disclosure_checked = disclosed  # already recorded from the carrier's playback
        self._opening_sent = False  # the forced opening response goes out exactly once
        self._opening_sent_at = 0.0
        self._opening_resp = ""  # id of the opening response, once the server created it
        self._opening_done = False  # barge-in and auto replies stay off until this is True
        # hold: while a question is pending the model never auto-replies (we send a short line)
        self._hold_vad = False
        self._last_hold_at = 0.0
        # lifecycle
        self.wrapup_sent = False
        self.end_requested_at: float | None = None
        self._end_pending = False      # end_call received; decide on response.done
        self._end_pending_at = 0.0
        self._goodbye_said = False     # agent thanked / said goodbye since the callee last spoke
        self.hung_up = False
        self.finish_reason = ""
        self._inbox: deque[tuple[str, str]] = deque()  # thread-safe hand-off from HTTP handlers
        # mid-call question (at most one open at a time)
        self._question_task: asyncio.Task | None = None
        self._question_id = ""
        # spoken callback line after a question timeout: hang up only once it was played
        self._closing_since: float | None = None
        self._closing_resp = ""
        self._closing_done = False

    # ---- public --------------------------------------------------------------------------
    def notify_voicemail(self, answered_by: str) -> None:
        self._inbox.append(("voicemail", answered_by))

    def notify_stream_lost(self) -> None:
        self._inbox.append(("stream_lost", ""))

    async def run(self, start: dict) -> None:
        self.stream_sid = start.get("streamSid", "")
        self.call_sid = start.get("callSid") or self.call_sid
        self.started_at = self.clock()
        try:
            self.conn = await self.connect(self.settings, self.call_id)
            await self.conn.send(rt.session_update(self.settings, self.payload))
            # The opener waits for the callee's greeting (see _send_opening / _ticker).
        except Exception:  # noqa: BLE001
            log.exception("realtime connect failed for %s", self.call_id)
            await self._finish(states.FAILED, "voice_unavailable", "failed",
                               "Could not connect to the voice model.", apology=True)
            return
        tasks = [asyncio.create_task(self._twilio_reader()),
                 asyncio.create_task(self._realtime_reader()),
                 asyncio.create_task(self._ticker())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if self._question_task is not None:
                tasks.append(self._question_task)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._close_open_questions()
            if self.conn is not None:
                await self.conn.close()
            self._stamp_duration()

    # ---- readers -------------------------------------------------------------------------
    async def _twilio_reader(self) -> None:
        while True:
            try:
                raw = await self.ws.receive_text()
            except Exception:  # noqa: BLE001 - disconnect
                await self._finish(states.COMPLETED, "callee_hung_up", "needs_followup",
                                   "The call ended without a recorded result.", rest=False)
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            kind = msg.get("event")
            if kind == "media":
                media = msg.get("media") or {}
                try:
                    self.latest_media_ts = int(media.get("timestamp", self.latest_media_ts))
                except (TypeError, ValueError):
                    pass
                if media.get("track", "inbound") == "inbound" and media.get("payload"):
                    await self._rt_send({"type": "input_audio_buffer.append",
                                         "audio": media["payload"]})
            elif kind == "mark":
                self.marks.discard((msg.get("mark") or {}).get("name", ""))
            elif kind == "stop":
                await self._finish(states.COMPLETED, "callee_hung_up", "needs_followup",
                                   "The call ended without a recorded result.", rest=False)
                return

    async def _realtime_reader(self) -> None:
        assert self.conn is not None
        while True:
            ev = await self.conn.recv()
            if ev is None:
                if not self.hung_up:
                    await self._finish(states.FAILED, "voice_connection_lost", "failed",
                                       "The voice connection dropped.", apology=True)
                return
            await self._on_realtime_event(ev)

    async def _on_realtime_event(self, ev: dict) -> None:
        etype = ev.get("type", "")
        if etype == "response.created":
            self.response_active = True
            if self._opening_sent and not self._opening_resp and not self._opening_done:
                self._opening_resp = str((ev.get("response") or {}).get("id") or "?")
            if self._closing_since is not None and not self._closing_resp:
                self._closing_resp = str((ev.get("response") or {}).get("id") or "?")
        elif etype == "response.done":
            self.response_active = False
            rid = str((ev.get("response") or {}).get("id") or "?")
            if self._opening_resp and rid == self._opening_resp and not self._opening_done:
                await self._opening_finished()
            if self._closing_resp and rid == self._closing_resp:
                self._closing_done = True
            if self._end_pending:
                await self._finish_politely()
        elif etype == "response.output_audio.delta":
            await self._on_audio_delta(ev)
        elif etype == "response.output_audio_transcript.done":
            self._line("agent", ev.get("transcript", ""))
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._line("callee", ev.get("transcript", ""))
        elif etype == "input_audio_buffer.speech_stopped":
            await self._send_opening()  # the callee finished their greeting
            if self._opening_done and self.question_open and not self.hung_up:
                await self._hold_reply()  # the callee spoke while we wait for the user
        elif etype == "input_audio_buffer.speech_started":
            await self._barge_in()
        elif etype == "response.function_call_arguments.done":
            await self._on_function_call(ev)
        elif etype == "error":
            err = ev.get("error") or {}
            self.store.add_event(self.call_id, "realtime_error",
                                 {"code": str(err.get("code", ""))[:80],
                                  "message": str(err.get("message", ""))[:200]})

    def _ctx(self, step: str) -> str:
        """Per-response instructions REPLACE the session instructions in OpenAI Realtime, so
        every override carries the full call context (who, what for, facts, rules) plus the step.
        Without this the opening said "I don't have the details yet" (live call, Nov 5)."""
        return rt.build_instructions(self.payload) + "\n\nCURRENT STEP (do this now):\n" + step

    async def _send_opening(self) -> None:
        """Forced opening (AI disclosure first), once: after the greeting or a silent timeout."""
        if self._opening_sent:
            return
        self._opening_sent = True
        self._opening_sent_at = self.clock()
        step = rt.opening_response_instructions(self.payload["opening_line"])
        await self._rt_send({"type": "response.create", "response": {
            "instructions": self._ctx(step)}})
        # VAD stays unable to answer or interrupt until this response is done (_opening_finished):
        # a second "Hey." must not cut the disclosure off and restart it.

    async def _opening_finished(self) -> None:
        if self._opening_done:
            return
        self._opening_done = True
        await self._rt_send(rt.turn_detection_update(
            create_response=not self._hold_vad, interrupt_response=True))

    # ---- audio ---------------------------------------------------------------------------
    async def _on_audio_delta(self, ev: dict) -> None:
        delta = ev.get("delta") or ""
        item = ev.get("item_id") or ""
        if not delta or not self.stream_sid:
            return
        if item != self.item_id:
            self.item_id, self.item_start_ts, self.item_sent_ms = item, self.latest_media_ts, 0.0
        try:
            self.item_sent_ms += len(base64.b64decode(delta)) / 8.0  # 8000 mu-law bytes/s
        except ValueError:
            return
        await self._tw_send({"event": "media", "streamSid": self.stream_sid,
                             "media": {"payload": delta}})
        self._mark_n += 1
        name = f"m{self._mark_n}"
        self.marks.add(name)
        await self._tw_send({"event": "mark", "streamSid": self.stream_sid,
                             "mark": {"name": name}})

    async def _barge_in(self) -> None:
        """Callee started talking: stop playback, cancel the response, trim what was unheard."""
        if self._opening_sent and not self._opening_done:
            return  # the opening (AI disclosure) is never interrupted or restarted
        await self._tw_send({"event": "clear", "streamSid": self.stream_sid})
        if self.response_active:
            await self._rt_send({"type": "response.cancel"})
            self.response_active = False
        if self.item_id:
            played = max(0, self.latest_media_ts - self.item_start_ts)
            await self._rt_send({"type": "conversation.item.truncate", "item_id": self.item_id,
                                 "content_index": 0,
                                 "audio_end_ms": int(min(played, self.item_sent_ms))})
        self.item_id = ""
        self.marks.clear()

    # ---- tools ---------------------------------------------------------------------------
    async def _on_function_call(self, ev: dict) -> None:
        name = ev.get("name", "")
        call_id = ev.get("call_id", "")
        respond = True
        try:
            args = json.loads(ev.get("arguments") or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError:
            output: dict = {"error": "invalid arguments"}
        else:
            if name == "end_call":
                output = self._tool_end_call(args)
                respond = False  # the model already said goodbye; do not talk after end_call
            elif name == "ask_user":
                await self._on_ask_user(call_id, args)
                return  # the output is sent later (answer, decline or timeout)
            else:
                output = {"error": "unknown tool"}
        await self._rt_send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(output)}})
        if respond:
            await self._rt_send({"type": "response.create"})

    # ---- ask_user: relay a question to the user while the call is on hold -------------------
    @property
    def question_open(self) -> bool:
        return self._question_task is not None and not self._question_task.done()

    def _consents(self) -> set[str]:
        return self.store.consents(self.call_id)

    async def _hold_start(self) -> None:
        """Pending question: VAD must not auto-create (chatty, leaky) replies; we send our own."""
        self._hold_vad = True
        self._last_hold_at = self.clock()
        if self._opening_done:
            await self._rt_send(rt.turn_detection_update(create_response=False))

    async def _hold_end(self) -> None:
        if not self._hold_vad:
            return
        self._hold_vad = False
        if self._opening_done:
            await self._rt_send(rt.turn_detection_update(create_response=True))

    async def _hold_reply(self) -> None:
        if self.response_active:
            return
        self._last_hold_at = self.clock()
        self.store.add_event(self.call_id, "hold_reply", {"question_id": self._question_id})
        await self._rt_send({"type": "response.create", "response": {
            "instructions": self._ctx(rt.HOLD_REPLY_INSTRUCTIONS), "tool_choice": "none"}})

    async def _answer_fn(self, fn_call_id: str, output: dict, instructions: str = "",
                         *, end_hold: bool = True) -> None:
        """Send the function_call_output and let the model speak. Never overlaps a response."""
        if end_hold:
            await self._hold_end()
        if self.response_active:
            await self._rt_send({"type": "response.cancel"})
            self.response_active = False
        await self._rt_send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": fn_call_id,
            "output": json.dumps(output)}})
        event: dict = {"type": "response.create"}
        if instructions:
            event["response"] = {"instructions": self._ctx(instructions)}
        await self._rt_send(event)

    def _set_state(self, new: str) -> None:
        cur = self.store.get_call(self.call_id)
        if cur is not None and states.can_transition(cur["state"], new):
            try:
                states.transition(self.store, self.call_id, new)
            except states.IllegalTransition:
                pass

    async def _on_ask_user(self, fn_call_id: str, args: dict) -> None:
        question = str(args.get("question") or "").strip()[:MAX_Q_LEN]
        if not question:
            await self._answer_fn(fn_call_id, {"error": "question is required"})
            return
        field = str(args.get("field") or "").strip()[:80]
        sensitive = consent.classify_question(field, question)
        if sensitive in consent.FORBIDDEN_FIELDS:  # never relayed, never asked
            self.store.add_event(self.call_id, "question_auto_declined", {"field": sensitive})
            await self._answer_fn(fn_call_id, {"declined": True, "reason": "never shared"},
                                  rt.DECLINED_INSTRUCTIONS)
            return
        if self.question_open:
            await self._answer_fn(fn_call_id, {
                "status": "busy", "note": "Another question to the user is still pending. "
                "Tell the callee you are still checking and wait; do not ask again."},
                end_hold=False)
            return
        granted = self._consents()
        needs_consent = sensitive is not None and sensitive not in granted
        suggested = str(args.get("suggested_answer") or "").strip()[:MAX_Q_LEN]
        if needs_consent or (suggested and consent.text_fields(suggested, granted)):
            suggested = ""  # a sensitive value is never suggested without consent
        qid = "q" + secrets.token_hex(12)
        self.store.add_question(qid, self.call_id, question, field, suggested,
                               kind="consent" if needs_consent else "question",
                               consent_field=sensitive or "" if needs_consent else "")
        self.store.add_event(self.call_id, "question_asked", {
            "question_id": qid, "kind": "consent" if needs_consent else "question"})
        self._set_state(states.ON_HOLD_FOR_USER)
        self._question_id = qid
        await self._hold_start()
        self._question_task = asyncio.create_task(self._await_answer(
            qid, fn_call_id, sensitive if needs_consent else ""))

    def _question_timeout_s(self) -> float:
        raw = self.call["brief"]["limits"].get("question_timeout_s",
                                               self.settings.question_timeout_s)
        return float(briefmod.clamp_question_timeout(raw))

    async def _await_answer(self, qid: str, fn_call_id: str, consent_field: str) -> None:
        try:
            started = self.clock()
            deadline = started + self._question_timeout_s()
            next_filler = started + self.settings.filler_every_s
            while not self.hung_up:
                q = self.store.get_question(qid)
                status = q["status"] if q else "timeout"
                if status == "answered":
                    await self._deliver_answer(qid, fn_call_id, q or {}, consent_field)
                    return
                if status == "declined":
                    self._set_state(states.IN_PROGRESS)
                    await self._answer_fn(fn_call_id, {"declined": True},
                                          rt.DECLINED_INSTRUCTIONS)
                    return
                if status != "open":  # closed elsewhere (timeout/late): nothing to inject
                    return
                now = self.clock()
                if now >= deadline:
                    if self.store.resolve_question(qid, "timeout") == "timeout":
                        await self._on_question_timeout(qid, fn_call_id, q or {})
                        return
                    continue  # the user answered in the same instant: read it next loop
                next_filler = max(next_filler, self._last_hold_at + self.settings.filler_every_s)
                if now >= next_filler and not self.response_active:
                    next_filler = now + self.settings.filler_every_s
                    self._last_hold_at = now
                    self.store.add_event(self.call_id, "hold_filler", {"question_id": qid})
                    await self._rt_send({"type": "response.create", "response": {
                        "instructions": self._ctx(rt.FILLER_INSTRUCTIONS), "tool_choice": "none"}})
                await asyncio.sleep(self.settings.question_poll_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("question wait failed for %s", self.call_id)

    async def _deliver_answer(self, qid: str, fn_call_id: str, q: dict,
                              consent_field: str) -> None:
        answer = str(q.get("answer") or "")
        granted = self._consents()
        # Defence in depth: the plugin checked; the bridge re-checks before the model sees it.
        blocked = (consent_field and consent_field not in granted) or consent.text_fields(
            answer, granted)
        self._set_state(states.IN_PROGRESS)
        if blocked or not answer:
            self.store.add_event(self.call_id, "answer_withheld", {"question_id": qid})
            await self._answer_fn(fn_call_id, {"declined": True, "reason": "no consent"},
                                  rt.DECLINED_INSTRUCTIONS)
            return
        self.store.add_event(self.call_id, "question_answered", {"question_id": qid})
        await self._answer_fn(fn_call_id, {"answer": answer}, rt.ANSWER_INSTRUCTIONS)

    async def _on_question_timeout(self, qid: str, fn_call_id: str, q: dict) -> None:
        self.store.add_event(self.call_id, "question_timeout", {"question_id": qid})
        field = "sensitive information" if q.get("kind") == "consent" else "an answer"
        self.store.set_result_once(self.call_id, {
            "outcome": "needs_followup", "reason": "question_timeout",
            "summary": f"The user did not provide {field} in time; the callee was told "
                       "we will call back.",
            "question_id": qid, "duration_s": int(self.clock() - self.started_at)},
            "needs_followup")
        self._closing_since = self.clock()  # before sending: its response events may race us
        await self._answer_fn(fn_call_id, {"timeout": True}, rt.TIMEOUT_INSTRUCTIONS)

    def _close_open_questions(self) -> None:
        """The call is over: nobody is waiting any more, so an answer now is a late one."""
        try:
            for q in self.store.questions_for_call(self.call_id):
                if q["status"] == "open":
                    self.store.resolve_question(q["id"], "timeout")
        except Exception:  # noqa: BLE001
            log.exception("could not close open questions")

    async def _finish_politely(self) -> None:
        """end_call arrived: never hang up in silence. If the agent did not thank the callee and
        say goodbye, make it say one short closing line first, then hang up after it played."""
        self._end_pending = False
        if self._goodbye_said:
            if self.end_requested_at is None:
                self.end_requested_at = self.clock()
            return
        self._closing_since = self.clock()  # before sending: its response events may race us
        await self._rt_send({"type": "response.create", "response": {
            "instructions": self._ctx(rt.GOODBYE_INSTRUCTIONS), "tool_choice": "none"}})

    def _tool_end_call(self, args: dict) -> dict:
        outcome = str(args.get("outcome") or "")
        if outcome not in rt.END_OUTCOMES:
            outcome = "needs_followup"
        agreed = args.get("agreed") if isinstance(args.get("agreed"), dict) else {}
        result = {"outcome": outcome, "agreed": agreed,
                  "next_steps": str(args.get("next_steps") or "")[:1000],
                  "duration_s": int(self.clock() - self.started_at)}
        self.store.set_result_once(self.call_id, result, outcome)
        if self.end_requested_at is None and self._closing_since is None:
            self._end_pending = True  # hang up after this response, with a goodbye guaranteed
            self._end_pending_at = self.clock()
        return {"status": "ok"}

    # ---- lifecycle -----------------------------------------------------------------------
    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(self.settings.tick_s)
            now = self.clock()
            elapsed = now - self.started_at
            if (self._opening_sent and not self._opening_done
                    and now - self._opening_sent_at >= self.settings.closing_max_s):
                await self._opening_finished()  # response.done never arrived: do not stay deaf
            if not self._opening_sent and elapsed >= self.settings.opening_wait_s:
                await self._send_opening()  # nobody said hello: speak first
            if self._end_pending and now - self._end_pending_at >= self.settings.end_grace_s:
                await self._finish_politely()  # response.done never came: still close politely
            while self._inbox:
                kind, value = self._inbox.popleft()
                if kind == "voicemail":
                    await self._on_voicemail(value)
                elif kind == "stream_lost":
                    await self._finish(states.FAILED, "stream_lost", "failed",
                                       "The call audio was lost.")
                    return
            if self.store.hangup_requested(self.call_id):
                await self._finish(states.ABORTED, "user_hangup", "needs_followup",
                                   "The user ended the call.")
                return
            if self._closing_since is not None and self.end_requested_at is None and (
                    self._closing_done or now - self._closing_since >= self.settings.closing_max_s):
                self.end_requested_at = now  # callback line played: hang up after the last mark
            if elapsed >= self.max_duration_s:
                await self._finish(states.ABORTED, "max_duration", "needs_followup",
                                   "The call reached its maximum duration.")
                return
            wrap_at = (self.max_duration_s - self.settings.wrapup_lead_s
                       if self.max_duration_s >= 60 else self.max_duration_s / 2)
            if (elapsed >= wrap_at and not self.wrapup_sent and not self.question_open
                    and self._closing_since is None):
                self.wrapup_sent = True
                self.store.add_event(self.call_id, "wrapup", {"elapsed_s": int(elapsed)})
                await self._rt_send({"type": "response.create", "response": {
                    "instructions": self._ctx(rt.WRAPUP_INSTRUCTIONS)}})
            if self.end_requested_at is not None:
                idle = not self.response_active and not self.marks
                if idle or now - self.end_requested_at >= self.settings.end_grace_s:
                    await self._finish(states.COMPLETED, "end_call", "")
                    return

    async def _on_voicemail(self, answered_by: str) -> None:
        if not self.payload.get("leave_voicemail"):
            await self._finish(states.VOICEMAIL, "voicemail", "voicemail",
                               "Reached voicemail, did not leave a message.")
        elif twilio_io.machine_message_done(answered_by) and not self.wrapup_sent:
            self.wrapup_sent = True
            step = rt.VOICEMAIL_INSTRUCTIONS
            if not self._opening_sent and self.payload["opening_line"]:  # disclosure goes first
                self._opening_sent, self._opening_sent_at = True, self.clock()
                step = (f"First say exactly: \"{self.payload['opening_line']}\" Then: " + step)
            await self._rt_send({"type": "response.create", "response": {
                "instructions": self._ctx(step)}})

    async def _finish(self, final: str, reason: str, outcome: str, summary: str = "",
                      *, rest: bool = True, apology: bool = False) -> None:
        """Settle state and result once, then hang up the line (unless the callee already did)."""
        if self.hung_up:
            return
        self.hung_up = True
        self.finish_reason = reason
        self._stamp_duration()
        if summary:
            self.store.set_result_once(
                self.call_id, {"outcome": outcome, "summary": summary, "reason": reason,
                               "duration_s": int(self.clock() - self.started_at)}, outcome)
        cur = self.store.get_call(self.call_id)
        if cur is not None and states.can_transition(cur["state"], final):
            fields: dict[str, Any] = {"reason": reason}
            if outcome and not cur.get("outcome"):
                fields["outcome"] = outcome
            states.transition(self.store, self.call_id, final, **fields)
        self.store.add_event(self.call_id, "bridge_finish", {"reason": reason})
        if rest:
            await self._hangup_rest(apology)

    def _stamp_duration(self) -> None:
        try:
            self.store.update_call(self.call_id,
                                   duration_s=int(self.clock() - self.started_at))
        except Exception:  # noqa: BLE001
            log.exception("could not stamp duration")

    async def _hangup_rest(self, apology: bool) -> None:
        if self.web:  # no Twilio leg: tell the page, then drop its websocket
            await self._tw_send({"event": "ended", "reason": self.finish_reason})
            try:
                await self.ws.close(code=1000)
            except Exception:  # noqa: BLE001
                pass
            return
        sid = self.call_sid
        if not sid:
            return

        def _do() -> None:
            client = self._client_factory()
            if apology:
                try:
                    client.calls(sid).update(twiml=twilio_io.say_and_hangup_twiml(
                        "Sorry, we are having technical problems. Goodbye."))
                    return
                except Exception:  # noqa: BLE001
                    log.warning("apology twiml failed; hanging up")
            client.calls(sid).update(status="completed")

        try:
            await asyncio.to_thread(_do)
        except Exception:  # noqa: BLE001
            log.exception("twilio hangup failed for %s", self.call_id)
            self.store.add_event(self.call_id, "hangup_failed", {})

    # ---- plumbing ------------------------------------------------------------------------
    def _line(self, role: str, text: str) -> None:
        text = (text or "").strip()
        if text and role == "agent" and rt.is_goodbye(text):
            self._goodbye_said = True
        elif text and role == "callee":
            self._goodbye_said = False
        if text and role == "agent" and not self._disclosure_checked:
            self._disclosure_checked = True  # first agent line only; independent of storage
            try:
                self.store.set_disclosure(self.call_id, briefmod.disclosure_in(text))
            except Exception:  # noqa: BLE001
                log.exception("could not record the disclosure check")
        if text and self.settings.store_transcripts:
            self.store.add_transcript_line(self.call_id, role, text)
            if self.web:  # the page shows a small live transcript
                asyncio.ensure_future(self._tw_send({"event": "transcript", "role": role,
                                                     "text": text[:500]}))

    async def _rt_send(self, event: dict) -> None:
        if self.conn is None or self.hung_up and event.get("type") != "response.cancel":
            return
        try:
            await self.conn.send(event)
        except Exception:  # noqa: BLE001
            log.warning("realtime send failed")

    async def _tw_send(self, msg: dict) -> None:
        try:
            await self.ws.send_text(json.dumps(msg))
        except Exception:  # noqa: BLE001
            pass
