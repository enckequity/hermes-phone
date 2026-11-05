# PROGRESS

## P0 scaffold + P1 approval and safety (2026-11-04)

Done and unit tested (234 tests, no network, ruff clean):

- Layout: root `__init__.py` (Hermes entry, re-exports `register`) + `hermes_phone/` package
  (`store`, `safety`, `consent`, `brief`, `states`, `tools`, `hooks`, `commands`). Verified against
  `plugins_loader.py`: the plugin dir is loaded as a package by path, relative imports work.
- `register(ctx)`: 4 tools, 2 hooks (`pre_tool_call`, `pre_llm_call`), `/phone`, bundled skill. No
  sockets, no `phonenumbers`/twilio imports, SQLite opened lazily.
- Approval gate in three places: `/phone approve` records one-use approval (15 min TTL, bound to
  call id, user, session, timestamp); `pre_tool_call` hook blocks; `phone_call` re-checks and also
  re-runs blocklist, allowlist, quiet hours and rate limits. Hook fails closed.
- Safety: E.164 + hard blocklist (bypass-tested), fail-closed allowlist (`HERMES_PHONE_ALLOW_ALL=1`
  is the only developer opt-out), quiet hours 08:00 to 21:00 (fail closed on unknown zone),
  rate limits with an injectable clock.
- Consent: per-call per-field consent via `/phone consent`; cvv/password/otp never shared; the
  voice payload (`brief.voice_payload`) is the only thing P2 may feed to the voice model.
- Tests cover SPEC acceptance 1, 2, 3, 7, state machine, TTL/one-use, loader-by-path.

Bug found by tests: quiet hours silently failed open because `phonenumbers.timezone` was never
imported; fixed and made fail-closed.

Doc corrections: HERMES_API.md facts folded into ARCHITECTURE/TESTING/START_PROMPT/PLAN
(entry point, no ctx secret accessor, `validate` has no `--ci`, no ctx cron API, `ask_choice`
returns None when unavailable).

Not done / open: `hermes plugins validate` and `doctor --ci` were not run (Hermes is
deliberately not installed here); run them once Hermes is available. `NullDialer` leaves a call in
`dialing`, which blocks further calls (1 active call) until P2 reports status.

## P2 media bridge (2026-11-05)

Done and unit tested (278 tests, no network, ruff clean):

- `hermes_phone/bridge/` FastAPI app (`create_app(settings, *, store, realtime_connect,
  twilio_client, clock)`), run by `python -m hermes_phone.bridge` or `hermes phone serve`.
  `/healthz`, signed `/twiml/{id}` (refuses unless dialing/ringing/in_progress with a consumed
  approval, re-checks blocklist and allowlist, mints a single-use 60 s token stored hashed in
  `media_tokens`), signed `/status/{id}` (idempotent mapping to SPEC states, AMD to voicemail, one
  result via `Store.set_result_once`, duration stored), `WS /media/{id}` (token from
  `start.customParameters`, base64 mu-law passthrough, barge-in with clear + response.cancel +
  truncate, transcripts, `end_call` handled, `ask_user` stub with TODO(P3), wrap-up at T-30 and
  hangup at T via injectable clock, Realtime loss fails the call with a spoken apology via Twilio
  `<Say><Hangup/>` update).
- Store schema v2: `calls.duration_s`, `media_tokens`, `set_result_once`.
- `TwilioDialer` (lazy twilio import, own blocklist and allowlist re-check, async AMD,
  time_limit = max + 15 s) and `PhoneService` dialer selection from env. `phone_call` fails closed
  (approval not spent) when Twilio is not configured or when `GET PUBLIC_BASE_URL/healthz` fails.
- `hermes phone serve` and `hermes phone doctor` via `register_cli_command` (register stays
  network-free). plugin.yaml: websockets>=14, httpx, `model` and `voice` config.
- Verified API facts and what could not be opened are in `docs/REFERENCES.md`.

Not done / open: no real call has been placed (needs keys and the developer's phone). Realtime
event shapes for GA were not re-read from OpenAI's own pages (sandbox blocked the domain); run one
real call before relying on them. `ask_user` only records a question; P3 adds hold, filler audio,
waiting, answer and timeout. The plugin does not yet spawn the bridge itself; run it with
`hermes phone serve` in the same container so both share the SQLite DB.

## P3 question relay (2026-11-05)

Done and unit tested (320 tests, no network, ruff clean):

- Bridge `ask_user`: the function call stays pending (no output yet). Row in `questions`
  (open), call goes `on_hold_for_user`, a separate asyncio task polls the DB every 0.25 s,
  hold filler every 15 s (`response.create` with `tool_choice: none`, never while a response is
  in flight, never the answer), answer -> `function_call_output {"answer"}` + `response.create`
  (a speaking filler is cancelled first), decline -> `{"declined": true}`, timeout
  (`question_timeout_s`, 75 default, clamped 30..120, stored in `brief.limits`) ->
  `{"timeout": true}`, the callback line, result stored once as `needs_followup`, hang up
  only after that line was played. One open question per call (`{"status":"busy"}` otherwise).
  Late answers are stored with status `late`, never injected.
- Sensitive handling: CVV/password/OTP (by field or by question text) are answered
  `{"declined": true, "reason": "never shared"}` at once, never stored as a question and never
  relayed. Card/address/ID/bank/email/phone without consent become `kind=consent` cards with no
  suggestion; the value reaches the model only in the function_call_output after the user
  grants consent for that field (`/phone answer <qid> <value>` or `/phone consent <id> <field>`
  with the stored value). The bridge re-checks consent before injecting.
- Plugin: `hermes_phone/relay.py` (watcher thread bound with `contextvars.copy_context()`,
  cards, suggestions, answer, hangup). `phone_call` starts the watcher after a successful dial
  and refuses when `ctx.post_message` is missing. Watcher edits one status message and posts
  the card within one 0.5 s poll. `pre_llm_call` skips questions whose card was posted.
- `/phone answer`, `/phone hangup` (DB mark -> bridge ticker hangs up via Twilio REST within
  one tick, state aborted / `user_hangup`; after 2 s without effect the plugin hangs up through
  `TwilioDialer.hangup` itself). Store schema v3.
- Tests: `tests/test_question_relay.py` covers SPEC criteria 5 and 6.

Deviation: Hermes' built-in `memory` tool has only add/patch/remove (no read), so the memory
suggestion calls `dispatch_tool("memory", {"action": "search", ...})` best-effort and skips
silently on error. In practice suggestions come from brief facts, `user_name`, and the voice
model's own guess. Not verified against a live Hermes or a real call.

## Web phone mode (2026-11-05)

Done and unit tested (366 tests, no network, ruff clean): browser page as the callee, no Twilio.

- `HERMES_PHONE_MODE=web` (or automatic when Twilio env is incomplete but `OPENAI_API_KEY` and
  `PUBLIC_BASE_URL` are set); `/phone check` and `hermes phone doctor` show the mode; the bridge
  no longer demands Twilio vars in web mode.
- `WebPhoneDialer`, store schema v4 (`web_tokens`), `GET /phone/{id}`, `POST /phone/{id}/answer`,
  self-contained page (mic -> 8 kHz mu-law 20 ms frames, scheduled playback, barge-in `clear`,
  mark echoes, live transcript), watcher posts the link, 60 s no-answer, hangup/end_call close the
  browser socket. Tests: `tests/test_web_phone.py` (includes a node check of the JS codec).
- Not verified in a real browser yet: run one call on Railway. Risks: iOS Safari audio unlock
  (AudioContext is created/resumed inside the Answer tap), `ScriptProcessorNode` is deprecated
  but universally supported, backgrounded tabs may throttle the mic, echo without headphones.

## P4 results (2026-11-05)

Done and unit tested (411 tests, no network, ruff clean):

- Exactly one result message per terminal state (success, partial, failed, needs_followup,
  no_answer, busy, voicemail, aborted/canceled/expired with the reason), posted by the watcher as
  a new message. `result_posted_at` makes it restart-safe; `pre_llm_call` reports only unposted
  results and skips calls with a live watcher. Example:
  `✅ Success. Tomorrow 19:00, party of 2, inside, under Winch. Confirmation B47. Next: nothing. 1m 12s, about $0.21.`
  then `Transcript: /phone transcript <id>`.
- Duration (Twilio CallDuration or bridge stamp) and a cost estimate (configurable
  `twilio_cost_per_min_usd` 0.014, `realtime_cost_per_min_usd` 0.15; web mode has no telephony cost).
- `/phone transcript` (chunks under 3500 chars, `Agent:`/`Them:`), `/phone log [n]`,
  `/phone retry <id>` (new call id, fresh approval, one per brief, late answers carried).
- `disclosure_spoken` recorded from the first agent line, shown by `phone_status`.
- Store schema v5. Tests: `tests/test_results.py`.

Deviations: `disclosure_spoken` is a `calls` column (not inside `result_json`) because the result
is written once and the first line is spoken earlier; `phone_status` exposes it at call level.
No retry scheduling (no plugin cron API): "retry later" is the manual `/phone retry`.
No-answer and busy results omit the transcript line (nothing was said).


## Conversation quality (first real PSTN call)
- The forced opening (AI disclosure first) now waits for the callee's greeting: sent on the first
  `input_audio_buffer.speech_stopped` or after `opening_wait_s` (2.5 s) of silence, exactly once.
  The initial `session.update` has `turn_detection.create_response=false` (so VAD cannot skip the
  disclosure); a follow-up `session.update` turns it back on after the opener.
- Opening instructions: exact opening line, then one first-person sentence on what the call is for;
  no "goal"/"brief" wording. Session rules 5/5b and the `end_call` description: confirm details,
  give the booking name, ask for a confirmation number, thank + goodbye, then `end_call`; a bare
  "OK"/"Book it" is not a confirmation; `success` only on clear confirmation.


## Live-demo fixes: names, plain-text replies, calmer voice
- Voice: `YOU ARE CALLING: <callee_name>` plus the full goal in the Realtime instructions;
  `phone_prepare` schema and SKILL.md ask for every non-sensitive detail (`person_to_see`,
  service, date, window, party size, booking name) in `facts_to_share`.
- Voice: the opening (AI disclosure) cannot be interrupted or restarted: the initial
  `turn_detection` has `create_response=false, interrupt_response=false` and both are enabled only
  when that response id is `response.done` (15 s fallback). Instructions: never repeat the
  introduction; an unknown detail means the hold line plus `ask_user` at once, never "I am not
  able to provide...". While a question is pending VAD auto-replies are off; callee speech gets one
  fixed short line; fillers are one of two fixed lines, at most every 15 s.
- Plugin: plain-text answers and approvals through `pre_llm_call` block (verified Hermes
  semantics in HERMES_API.md; safety reasoning in ARCHITECTURE.md). Schema v6 adds
  `calls.origin_session_id` and `questions.card_suggestion`.
- New cards: question `❓ X asks: "..." Suggested: 1 - reply "yes" ...`; approval card ends with
  `Reply "call" to place it, "cancel" to drop it (or /phone approve <id>).`; the live status
  message's final edit is "Call finished - result below." (it is edited in place, so it keeps its
  old timestamp; the result is a separate newer message).
- Tests: `tests/test_plain_reply.py`, `tests/test_bridge_hold.py`.
