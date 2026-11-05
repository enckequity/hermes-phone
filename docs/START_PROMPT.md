# Start prompt for Claude Code

Paste everything below the line into Claude Code, opened in an empty repo that contains this pack.

---

You are building `hermes-phone`, a native plugin for Nous Research Hermes Agent that lets the agent place real phone calls for the user, with explicit approval, live AI disclosure, mid-call questions relayed to the user, and a clean result at the end. It is an entry for the Hermes Agent Plugin Hackathon (deadline around Nov 11, 2026; judged on usefulness, reliability, creativity).

## Read first, in this order
1. `docs/SPEC.md` (user flow, states, edge cases, acceptance criteria). This is the contract.
2. `docs/ARCHITECTURE.md` (components, tool schemas, sequence diagrams).
3. `docs/PLAN.md` (phases and cut lines), `docs/TESTING.md`, `docs/SUBMISSION.md`.
4. `docs/REFERENCES.md`. Before using any Hermes, Twilio or OpenAI API, open the cited page and confirm the exact signature. Anything marked UNVERIFIED must be checked against the live docs or the Hermes source (`git clone https://github.com/NousResearch/hermes-agent`) before you rely on it. If reality differs from the docs here, follow reality and update the doc.
5. `skeleton/` holds starting points only (plugin.yaml, __init__.py, pyproject.toml, .env.example). Move them to the repo root and grow them (done; the package lives in `hermes_phone/` with a thin root `__init__.py`).

## Hard rules
- Never place a real outbound call except to the developer's own test number in `HERMES_PHONE_TEST_ALLOWLIST`. Until the developer says "allowlist off", the dialer must refuse every other number in code, not just in the prompt.
- No call without an explicit approval recorded for that exact call id. The `pre_tool_call` hook enforces this; the tool handler checks again (defense in depth).
- Never dial emergency or premium numbers (911, 112, 999, 000, 101, 1-900, etc). Hard blocklist in code with tests.
- The callee hears an AI disclosure in the first sentence of every call.
- Sensitive fields (card, address, ID numbers) are never sent to the voice model unless the user granted per-call consent for that field.
- Secrets only from env or Hermes secret scope. Never read `.env`, `auth.json` or other credential files directly. No self-updating code. Only public Hermes surfaces (`ctx.*`, hooks). No monkeypatching core.
- Tool handlers return JSON strings, never raise, accept `**kwargs`. Hook callbacks accept `**kwargs`.
- Use `plugin_data_dir("hermes-phone")` / `plugin_db("hermes-phone")` for call logs and transcripts, never the plugin directory.
- Pin upper bounds on every dependency.

## How to work
Build in the phases from `docs/PLAN.md`. For each phase:
1. Write or update tests first for the phase's acceptance criteria (see `docs/TESTING.md`).
2. Implement.
3. Run `pytest -q`, `ruff check`, then `hermes plugins validate . --install-deps` and `hermes plugins doctor . --ci` once the plugin loads (note: only `doctor` has `--ci`; `validate` always exits 0/1).
4. Report: what passed, what you changed in the docs, what is still open. Do not start the next phase while the current one is red.

Phases in short:
- P0 Scaffold: plugin loads, tools registered, doctor and validate green, no network in `register()`.
- P1 Approval and safety: phone_prepare brief, approval store, pre_tool_call hook, blocklist, allowlist, consent model. Fully unit tested, no Twilio needed.
- P2 Media bridge: FastAPI server with `/twiml`, `/status`, `/media` websocket bridging Twilio Media Streams (mu-law 8 kHz) to OpenAI Realtime (`audio/pcmu` in and out). Tested against a mock Twilio websocket client and a mock Realtime server.
- P3 Question relay: `ask_user` function tool in the Realtime session, hold filler, answer via `/phone answer`, 75 s timeout, graceful callback line.
- P4 Results: summary extraction, transcript storage, call log, phone_status/phone_transcript, no-answer/busy/voicemail handling, retry offer via Hermes cron after user ok.
- P5 Simulated callee E2E: second Realtime agent plays a restaurant host; full flow runs without a phone network.
- P6 Real call to the developer's own second phone, demo recording, README, catalog YAML.

Start now with P0. First, print a short list of the Hermes APIs you will use and confirm each one against the docs or source.
