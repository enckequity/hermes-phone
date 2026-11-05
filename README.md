# hermes-phone

> **hermes-phone** lets your Hermes agent make the phone calls you keep putting off. Say "book a table" or "ask the clinic if they have a slot Friday", check the brief, tap Call. The agent introduces itself as an AI, handles the conversation, and when the other side asks something it doesn't know, it puts them on a polite hold and asks you in chat, with a suggested answer ready. You get the result, the confirmation number and the transcript. No call without your approval, no surprise sharing of card or address, no auto-redials, emergency numbers blocked.
>
> Twilio Media Streams + OpenAI Realtime, native Hermes hooks, slash commands and live messages. Works from Telegram, Discord, Slack or the CLI.

Status: P0 (scaffold), P1 (approval and safety) and P2 (media bridge: Twilio Media Streams <->
OpenAI Realtime) are implemented and unit tested against mocks. No real call has been placed yet.
See `docs/PROGRESS.md`.

## Safety model

- **Approval gate.** `phone_prepare` only creates a brief card. `phone_call` is refused unless the
  user approved that exact call id with `/phone approve <id>` (Call / Edit / Cancel). Approval is
  one-use and expires after 15 minutes. Enforced in the `pre_tool_call` hook and again in the handler.
- **Hard blocklist.** Emergency numbers (911, 112, 999, 000, 110, 119, 100-103, ...), premium prefixes
  (1-900, 1-976) and anything with fewer than 6 national digits are refused at prepare and at dial.
  Not configurable.
- **Fail-closed allowlist.** Only numbers in `HERMES_PHONE_TEST_ALLOWLIST` (comma-separated E.164)
  can be dialed. If it is unset or empty, **all dialing is refused**. A developer can disable the
  allowlist by exporting `HERMES_PHONE_ALLOW_ALL=1` in the environment; the model has no way to set it.
- **Quiet hours and rate limits.** 08:00 to 21:00 callee local time, 1 active call, daily limit
  (default 10), 60 s between calls to the same number.
- **Sensitive data.** card, address, id_number, bank, email and phone values are withheld from the
  voice model unless you grant per-call consent (`/phone consent <id> <field>`). CVV, passwords and
  OTP codes are never shared.
- **AI disclosure** is the first sentence of every call.
- **Slash commands.** `/phone approve <id>` (buttons, or `/phone approve <id> confirm` where
  buttons are unavailable), `cancel`, `consent`, `status`, `transcript`, `log`. `answer` and
  `hangup` arrive in a later phase.

## Web phone mode (no Twilio)

No telephony provider, no phone number to buy. The "callee" is a page in your browser: the agent
"rings" by posting a link in your chat, you open it, tap **Answer**, and talk to the agent through
your mic and speakers. The whole pipeline is the same as a real call (AI disclosure first, approval
card, allowlist, quiet hours, rate limits, mid-call questions to the chat, transcript, max
duration). The number you give is still validated and allowlisted, but nothing is dialed.

Minimal Railway variables:

| Variable | Value |
|---|---|
| `HERMES_PHONE_MODE` | `web` (optional: it is also picked automatically when Twilio is not fully configured) |
| `OPENAI_API_KEY` | your OpenAI key (Realtime voice) |
| `PUBLIC_BASE_URL` | `https://<your-railway-domain>` (no trailing slash) |
| `HERMES_PHONE_TEST_ALLOWLIST` | any valid number to "call", e.g. `+12125550123` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS` | your bot and your numeric Telegram id |
| `GEMINI_API_KEY` (or another LLM key, see below) | the agent's brain |

To test: open the Telegram chat and send `/phone check` (it prints `mode: web`). Then ask the
agent, for example, "call Bella Napoli at +12125550123 and ask if they have a table for 2 tonight".
Tap **Call** on the approval card. A message arrives: "Ringing in your browser: open this link and
tap Answer", followed by a plain `https://.../phone/<id>?t=...` URL. Open it on your phone or
laptop within about a minute, tap **Answer**, allow the microphone, and answer as the restaurant.
The agent speaks first and says it is an AI. Hang up with the red button or `/phone hangup`.
Use headphones to avoid echo. The link works once; if it expires you get `no_answer` and nothing is
redialed.

Notes: iOS Safari needs the tap on Answer to start audio (the page does this), the screen should
stay on during the call, and the page needs https (Railway provides it).

## Running the bridge

The bridge is a small FastAPI server that connects the Twilio call audio to OpenAI Realtime. It
must run in the same container as Hermes because both read and write the same SQLite database.

```bash
export TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_FROM_NUMBER=+1...
export OPENAI_API_KEY=... HERMES_PHONE_TEST_ALLOWLIST=+1YOUR_TEST_NUMBER
hermes phone serve                 # or: python -m hermes_phone.bridge  (default 127.0.0.1:8765)
cloudflared tunnel --url http://localhost:8765   # or: ngrok http 8765
export PUBLIC_BASE_URL=https://<your-tunnel-host>   # set before starting the bridge and Hermes
hermes phone doctor                # env names, allowlist, bridge health (never prints secrets)
```

Twilio signs every webhook; the bridge validates against `PUBLIC_BASE_URL`, so that value must be
the exact public https URL. Standalone runs read the database path from `HERMES_PHONE_DB_PATH`;
`hermes phone serve` uses the plugin database. Env: `HERMES_PHONE_BRIDGE_HOST` (default
127.0.0.1; with Railway's `$PORT` set the bridge binds 0.0.0.0), `HERMES_PHONE_BRIDGE_PORT`,
`HERMES_PHONE_MODEL` (default `gpt-realtime-2.1`), `HERMES_PHONE_VOICE`. See "Deploy on Railway".

## Deploy on Railway

The repo `Dockerfile` builds `nousresearch/hermes-agent:v0.22.7` plus this plugin and runs, in one
container, the Hermes gateway (Telegram, long polling) and the bridge (`/healthz`, Twilio webhooks,
media stream on `$PORT`). Both share one SQLite file, `/opt/data/plugin-data/hermes-phone/data.db`
(`HERMES_PHONE_DB` is baked into the image). The plugin is copied into
`/opt/data/plugins/hermes-phone` and enabled in `config.yaml` on every boot; the bridge is an s6
service of the base image, so it is restarted if it exits. Railway builds it on every push to main
(`railway.json`; CI also runs `docker build`).

Manual steps in Railway:

1. Add a **Volume** to the service, mount path `/opt/data` (Hermes home: config, sessions, the DB).
2. Settings, Networking, **Generate Domain**; set `PUBLIC_BASE_URL=https://<that domain>` (no
   trailing slash). Twilio signatures are validated against it.
3. Set the variables below, redeploy, then check `https://<domain>/healthz` returns `{"ok":true}`.
4. Set `PORT=8765` and point the generated domain at port **8765**. Railway's port picker may suggest 8642: that is the Hermes API server, never expose it publicly.

| Variable | Required | Purpose |
|---|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` | for Twilio mode | Twilio credentials and caller id (E.164) |
| `HERMES_PHONE_MODE` | optional | `web` (browser as the callee, no Twilio) or `twilio`; unset picks Twilio when fully configured, else web when `OPENAI_API_KEY` and `PUBLIC_BASE_URL` are set |
| `OPENAI_API_KEY` | for calls | OpenAI Realtime voice |
| `PUBLIC_BASE_URL` | for calls | Public https URL of the Railway domain |
| `HERMES_PHONE_TEST_ALLOWLIST` | for calls | Comma-separated E.164 numbers that may be dialed; empty means all dialing is refused |
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `TELEGRAM_ALLOWED_USERS` | yes | Comma-separated numeric Telegram user ids; unset denies everyone |
| `GEMINI_API_KEY` | one LLM key (free, primary) | Google AI Studio key (https://aistudio.google.com/app/apikey, free tier). With no provider set in `config.yaml` or `HERMES_INFERENCE_PROVIDER`, boot writes `model.provider: gemini` and `model.default: gemini-3.8-flash` |
| `HERMES_PHONE_LLM_MODEL` | optional | Overrides `model.default` for any provider, e.g. `gemini-3.7-flash` |
| `OPENROUTER_API_KEY` | alternative free path | Set `HERMES_INFERENCE_PROVIDER=openrouter` and `HERMES_PHONE_LLM_MODEL` to a `:free` model (e.g. `z-ai/glm-5.2:free`); `HERMES_PHONE_FREE_ONLY=1` sets `auxiliary.free_only` so background tasks never use a paid OpenRouter model |
| `OPENAI_API_KEY` as the brain too | simplest (paid, cheap) | Set `HERMES_INFERENCE_PROVIDER=openai` and `HERMES_PHONE_LLM_MODEL=gpt-5-mini`. Without an explicit provider, Hermes `auto` sees `OPENAI_API_KEY` (needed for Realtime) and pairs it with the image's non-OpenAI default model, which fails with HTTP 404 |
| `ANTHROPIC_API_KEY`, `NOUS_API_KEY`, ... | alternative (paid) | Any other Hermes provider; set `HERMES_INFERENCE_PROVIDER` |
| `HERMES_MODEL` | optional | Written to `model.default` (lower precedence than `HERMES_PHONE_LLM_MODEL`) |
| `HERMES_PHONE_USER_NAME` | recommended | Plugin setting `user_name` (who the agent calls on behalf of) |
| `HERMES_PHONE_CALLBACK_NUMBER` | optional | Plugin setting `callback_number` |
| `HERMES_PHONE_QUESTION_TIMEOUT_S`, `_MAX_DURATION_S`, `_DAILY_CALL_LIMIT`, `_VOICE`, `_MODEL` | optional | Other plugin settings |

At boot `deploy/boot-config.py` merges the plugin settings and model keys into
`/opt/data/config.yaml` (Hermes format; blank variables never overwrite; `auth.json` and `.env`
are never touched). If Twilio or OpenAI variables are missing the bridge still starts and
`/healthz` is OK, but every webhook is refused and `phone_call` fails closed. Secret values are
never logged; the Gemini free tier allows only a few hundred requests a day and Hermes makes several per turn, so expect quota errors under heavy use; missing variables are reported by name only. Only set secrets in Railway variables,
never in the image.

## Development

```bash
uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
.venv/bin/pytest -q -m "not e2e"
.venv/bin/ruff check .
```

With Hermes installed: `hermes plugins validate . --install-deps` and `hermes plugins doctor . --ci`.
