# hermes-phone TESTING

## Layers
1. **Unit** (pytest, no network): brief builder, state machine transitions, approval store (TTL, one-use), pre_tool_call hook (block without approval, pass with, block in cron without retry token), blocklist/allowlist/E.164 normalization (`phonenumbers` lib), consent filter (sensitive values never in instructions), rate limiter, result formatter, timeout logic with a fake clock.
2. **Bridge with mocks**:
   - **Mock Twilio**: a websocket client that sends `connected`, `start` (streamSid, callSid, customParameters, mediaFormat audio/x-mulaw 8000), `media` frames from a mu-law WAV fixture at 20 ms pacing, `mark` echoes, and `stop`. Also an httpx client posting signed status callbacks (`twilio.request_validator.RequestValidator` to compute signatures) for ringing, in-progress, completed, busy, no-answer, and AMD `AnsweredBy=machine_end_beep`.
   - **Mock Realtime**: a local websocket server that records `session.update`, replies with scripted `response.output_audio.delta`, `response.output_audio_transcript.delta`, and `response.function_call_arguments.done` for `ask_user` / `end_call`.
   - Assert: session.update uses `audio/pcmu` both ways; first response contains disclosure; `clear` sent on `input_audio_buffer.speech_started`; hangup on max duration.
   - Mock Twilio REST: patch `twilio.rest.Client` so `calls.create` and `calls(sid).update` are recorded, never sent.
3. **Simulated callee E2E** (costs API tokens, no phone network): run the bridge, replace Twilio with a "fake carrier" that connects the agent's audio stream to a second OpenAI Realtime session playing the callee (prompt: "You are a host at Bella Napoli. Ask whether inside or terrace, ask the name, give confirmation B47."). Both sides use `audio/pcmu` so you can pipe deltas directly. Auto-answer user questions from a fixture after N seconds, or never (timeout test). Assert result JSON fields and transcript. Mark `@pytest.mark.e2e`, run manually.
4. **Real call** to own second phone only (allowlist). Checklist: disclosure heard, latency acceptable (< 1.5 s turn), question relay works from Telegram, hangup clean, result posted, Twilio console shows one call.

## Hook tests (examples)
```python
def test_phone_call_blocked_without_approval(store):
    r = pre_tool_call(tool_name="phone_call", args={"call_id": "c1"}, task_id="t")
    assert r["action"] == "block"

def test_phone_call_passes_with_approval(store):
    store.approve("c1", user="u")
    assert pre_tool_call(tool_name="phone_call", args={"call_id": "c1"}, task_id="t") is None

def test_other_tools_untouched():
    assert pre_tool_call(tool_name="web_search", args={}, task_id="t") is None

def test_emergency_refused():
    assert json.loads(phone_prepare({"callee_name":"x","number":"911","goal":"y"}))["error"]
```

## Commands
```
pytest -q -m "not e2e"
pytest -q -m e2e          # needs OPENAI_API_KEY
ruff check .
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```
CI: GitHub Actions running ruff and the unit + mock layers on Python 3.11 and 3.12 (`.github/workflows/ci.yml`). `hermes plugins doctor` is not in CI yet because it needs Hermes installed; run it locally. Note `validate` has no `--ci` flag and does not check dependency upper bounds (doctor warns); `tests/test_register.py` enforces upper bounds itself.
