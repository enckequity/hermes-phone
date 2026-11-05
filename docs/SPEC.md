# hermes-phone SPEC

The contract for behavior. If code and this file disagree, fix one of them on purpose.

## 1. User flow

1. **Request.** User writes in plain language, for example "call Bella Napoli and book a table for 4 at 20:00 tonight". The agent finds the number with web search, or uses the number the user gave. If several candidates exist, it asks which one.
2. **Brief and approval.** The agent calls `phone_prepare`, which returns a brief card:
   - Who: business or person name
   - Number: E.164, plus source (user given or URL where it was found)
   - Goal: one sentence
   - Opening line: exactly what it will say first, including AI disclosure
   - Will share: e.g. name for the booking, party size, time
   - Won't share: card data, address, ID, anything not listed above
   - Limits: max duration, max cost estimate, recording on or off
   The card is shown with buttons **Call**, **Edit**, **Cancel** (Telegram/Discord/Slack buttons; numbered list on other platforms; picker in CLI). Only **Call** marks the call approved. No approval, no call. The `pre_tool_call` hook blocks `phone_call` for any call id without a recorded approval.
3. **On the call.** First sentence: "Hi, this is an AI assistant calling on behalf of {user_name}." Then it pursues the goal. If asked "am I talking to a robot?", it answers yes, always.
4. **Mid-call question.** When the callee asks something the agent cannot answer from the brief ("terrace or inside?", "name for the booking?"):
   - It says "One moment please, let me check." and plays a soft filler every ~15 s ("Thanks for waiting, still checking.").
   - The user gets a question card with a suggested answer prefilled from Hermes memory/profile when available, marked as a suggestion.
   - User confirms, edits or declines. The agent continues with the answer.
   - Timeout default 75 s (configurable 30 to 120): the agent says "Sorry, I could not confirm that right now. I'll call back. Thank you!" and ends the call politely, result `needs_followup`.
   - The agent never invents an answer.
5. **Sensitive data.** Card numbers, CVV, full address, ID or passport numbers, bank details, passwords and OTP codes are only spoken if the user granted consent for that specific field on that specific call. Mid-call requests for them trigger a separate consent card, even if the user approved the call. CVV and passwords/OTPs are never shared, consent or not (v1 rule).
6. **Result.** After hangup the user gets a short message: outcome (success / partial / failed / needs_followup), what was agreed (date, time, name, party size, price, confirmation number), next steps, duration, cost estimate. Full transcript via `phone_transcript` or `/phone transcript <id>`, stored in plugin data.
7. **No answer / busy / voicemail.** Reported plainly ("No answer after 30 s", "Line busy", "Reached voicemail, did not leave a message"). No automatic redial. The agent offers "Retry in 30 min?"; only on user ok does it create one Hermes cron job for one retry, which still requires the same approved brief (approval is per call id; a retry gets a fresh card unless the user ticked "approve retry with same brief").
   - Voicemail message: off by default. If enabled in the brief, leave one short message with AI disclosure and callback number, then hang up.

## 2. Call states

```
draft -> awaiting_approval -> approved -> dialing -> ringing -> in_progress
in_progress -> on_hold_for_user -> in_progress
in_progress -> completed
dialing|ringing -> no_answer | busy | failed | canceled
in_progress -> voicemail (AMD) -> completed
any active -> aborted (user /phone hangup, budget, max duration, safety stop)
awaiting_approval -> canceled | expired (approval TTL 15 min)
```
Terminal: completed, no_answer, busy, failed, canceled, expired, aborted. Each terminal state produces exactly one result message.

## 3. Limits and safety
- Per call: `max_duration_s` default 300, hard cap 900. Warning to callee at T-30 s ("I need to wrap up shortly"). Cost estimate shown in brief; abort if estimate exceeded.
- Rate limits: max 1 active call per user, max 10 calls/day per profile (configurable), min 60 s between calls to the same number, never more than 1 retry per brief.
- Blocklist: emergency numbers (911, 112, 999, 000, 110, 119, 100-103 IL, etc), premium/toll prefixes (1-900, 1-976), short codes under 6 digits. Hard fail, not configurable off.
- Dev allowlist: `HERMES_PHONE_TEST_ALLOWLIST` (comma-separated E.164). When set, only those numbers can be dialed. Default in dev.
- Quiet hours: refuse calls to businesses outside 08:00 to 21:00 callee local time unless user overrides (TCPA style courtesy; not legal advice).
- Recording: off by default. Opt-in per call. When on, the opening line adds "This call may be recorded." Transcripts are always kept (text from the model's transcription) unless `store_transcripts: false`.
- Kill switch: `/phone hangup` ends the active call immediately.

## 4. Edge cases
- Callee interrupts (barge-in): send Twilio `clear`, cancel the Realtime response.
- IVR menu: agent may send DTMF digits it hears requested (bridge supports `<Play digits>` via call update) only if they match the goal; log every digit. UNVERIFIED approach, P4 stretch.
- Callee transfers or puts agent on hold music: keep listening up to `max_hold_s` (120), then treat as needs_followup.
- Callee speaks another language: the agent switches if confident; brief language is stored.
- Network loss to Realtime: say "Sorry, I'm having connection trouble, I'll call back." if possible, hang up, result failed with reason.
- User answers after timeout: store answer, mark it for the callback brief.
- Gateway not live (CLI only, user away): questions fall back to CLI notice; if no channel, the call is not allowed to start (preflight check: question relay must be reachable).
- Hermes restart mid-call: bridge keeps the call, writes results to the DB; on next session the plugin posts pending results.

## 5. Acceptance criteria
1. `phone_call` without approval is blocked by the hook and by the handler (unit test both).
2. Blocklisted number is refused at prepare and at dial (unit test).
3. With allowlist set, any other number is refused (unit test).
4. The first audio response of every call contains the AI disclosure (simulated callee E2E checks transcript).
5. Mid-call question reaches the user within 3 s of the model calling `ask_user`, with suggestion prefilled when memory has it; answer resumes the call within 2 s (E2E with mocks).
6. Question timeout ends the call with the callback line and `needs_followup` (E2E).
7. Sensitive field without consent is never in the Realtime session instructions or tool output (unit test scans payloads).
8. No-answer, busy, voicemail statuses produce one plain result message and no redial (mock Twilio status callbacks).
9. Max duration triggers wrap-up and hangup (mock clock).
10. Transcript and call log persist across plugin reload (plugin_db).
11. `hermes plugins validate . --install-deps` and `hermes plugins doctor . --ci` pass.
12. One real call to the developer's second phone completes the demo script end to end.
