# hermes-phone PLAN (Nov 5 to Nov 11, 2026)

Deadline is around Nov 11. Aim to submit on Nov 10 evening and keep Nov 11 as buffer.

| Day | Goal | Done when |
|---|---|---|
| Thu Nov 5 | P0 scaffold. Twilio trial account, buy/verify a number, verify own second phone as caller ID target, ngrok or cloudflared tunnel. | `hermes plugins validate` and `doctor --ci` green, 4 tools listed. |
| Fri Nov 6 | P1 approval and safety: phone_prepare, brief card, `/phone approve` with ask_choice, approvals table, pre_tool_call hook, blocklist, allowlist, consent model, rate limits. | Unit tests for criteria 1, 2, 3, 7 green. |
| Sat Nov 7 | P2 media bridge: /twiml, /status, /media; Realtime session with audio/pcmu; disclosure first; barge-in with `clear`. First real call to own phone with a fixed goal. | You hear the AI disclose and hold a short conversation. |
| Sun Nov 8 | P3 question relay: ask_user tool, filler, watcher thread with post_message, `/phone answer`, timeout path. | E2E mock test for criteria 5, 6 green; one real call with a question answered from Telegram. |
| Mon Nov 9 | P4 results: end_call extraction, result message, transcripts, call log, no answer/busy/voicemail (AMD), max duration, retry offer via cron. P5 simulated callee. | Criteria 4, 8, 9, 10 green. |
| Tue Nov 10 | P6 demo: record video, README, own repo public, tag, catalog YAML with 40-char SHA, PR. | PR open, video linked. |
| Wed Nov 11 | Buffer: fix review comments, security scan findings. | Merged or green. |

Note: HERMES_API.md found no ctx scheduling API; the cron retry (P4) must import `cron.jobs.create_job` directly or fall back to "say retry when ready".

## Cut lines (drop in this order if late)
1. Gemini Live and Pipecat backends (keep interface, ship OpenAI only).
2. DTMF / IVR navigation.
3. Voicemail message leaving (still detect and report voicemail).
4. Cron retry (just offer "say retry when ready").
5. Recording (keep transcripts only).
6. ask_choice buttons (fall back to `/phone approve <id>` and `/phone answer <id> <text>` text commands).

Never cut: approval gate, AI disclosure, blocklist/allowlist, question relay with timeout, result message. Those are the demo and the reliability score.

## Daily habit
End each day with: tests green, one commit, a 3-line note in `docs/PROGRESS.md` (create it), and any doc corrections from what you found in the real APIs.
