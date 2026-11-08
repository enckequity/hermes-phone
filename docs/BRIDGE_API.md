# Shared bridge API, v1

This is the contract between the hermes-phone plugin and a shared, multi-tenant bridge. The
plugin is the client and makes outbound HTTPS requests only; the workspace needs no public
address. When `HERMES_PHONE_BRIDGE_URL` is unset, the plugin works as upstream does (co-located
bridge, shared SQLite) and this API is not used.

Client: `hermes_phone/remote.py` (`RemoteBridge`). Tests: `tests/test_remote_bridge.py`.

## Configuration and auth

| Env | Meaning |
|---|---|
| `HERMES_PHONE_BRIDGE_URL` | Base URL, for example `https://phone.example.com`. Must be `https`; plain `http` is accepted only for `127.0.0.1`, `localhost` and `::1` (tests). |
| `HERMES_PHONE_BRIDGE_TOKEN` | Workspace credential, read through the profile-scoped secret lookup. Used when set. |
| `TEAMSHIFT_USAGE_TOKEN` + `TEAMSHIFT_WORKSPACE_NAME` | Fallback when the bridge token is unset: the workspace's usage token, sent with an `X-TeamShift-Workspace` header. |

Every request sends `Authorization: Bearer <token>`. The bridge works out the workspace from the
credential, so no request body names a workspace. The bridge accepts two credentials:

1. **Bridge token** `<workspace>.<sig>`, where `<workspace>` matches `^[a-z0-9][a-z0-9-]{0,62}$` and
   `sig = base64url_nopad(HMAC-SHA256(key=signing key, msg="hermes-bridge-v1|" + workspace))`.
   The bridge recomputes the signature and compares it in constant time; anything malformed,
   signed with another key or naming another workspace is `401`. Mint one with
   `hermes phone bridge-token <workspace>` (reads `HERMES_PHONE_BRIDGE_SIGNING_KEY`). The ghost
   bridge uses the same scheme with the context `ghost-bridge-v1|`, so its tokens do not work here.
2. **Usage token** `ts_usg_<64 hex>` plus `X-TeamShift-Workspace: t-<10 hex>`. The bridge checks the
   pair with `GET <HERMES_PHONE_BRIDGE_AUTH_URL>`, sending `Authorization: Bearer <token>` and
   `x-teamshift-workspace: <name>`. `200` means valid and is cached for 10 minutes (keyed by a
   hash of name and token); `401` is `401` and is remembered for 60 seconds; anything else,
   including a timeout, is `503 auth_unavailable` and is not cached. Bad shapes are `401`
   without a lookup. The bridge makes at most 120 lookups a minute in total (`429` past that)
   and 8 at a time (`503` when all 8 are busy). With the auth URL unset this path is off.

A workspace marked `disabled` in the overrides gets `403 workspace_disabled`.

Rate limits, answered with `429 rate_limited`: 60 requests a minute per client address,
counted before auth, and 60 a minute per verified workspace, counted after auth. The plugin
polls `updates` every 2 seconds, which stays well inside that.

A call id, question id or cursor that belongs to
another workspace is answered with `404`, exactly as if it did not exist. The client never
follows redirects, so the token is never sent to a second host.

All paths are under `/v1`. Bodies are JSON. Errors look like this:

```json
{"error": {"code": "approval_required", "message": "short, human-readable, no secrets"}}
```

The plugin shows the user `phone service error: HTTP <status>: <message>`, so messages must not
name vendors or include provider internals.

## Endpoints

### `GET /v1/health`
`200 {"ok": true}` when the bridge can take calls for this workspace. `401` for a bad token. The
plugin calls it before it spends the user's approval. If it fails, nothing is dialed and the
approval is kept.

### `PUT /v1/calls/{call_id}`: submit an approved call
The plugin sends this once, after its own approval checks pass and it has consumed the one-use
approval (`phone_call`).

```json
{
  "call_id": "c1a2b3c4",
  "number": "+12125550123",
  "approval": {"id": "ap_<32 hex>", "approved_at": 1762268400.0,
               "expires_at": 1762269300.0, "consumed_at": 1762268410.0},
  "brief": {"call_id": "c1a2b3c4", "callee_name": "Bella Napoli",
            "goal": "Book a table for 4", "opening_line": "Hi, this is an AI assistant ...",
            "facts": {"name": "Winch", "party_size": 4}, "language": "en",
            "max_duration_s": 300, "record": false, "leave_voicemail": false,
            "user_name": "Winch"},
  "limits": {"max_duration_s": 300, "question_timeout_s": 75},
  "options": {"store_transcripts": true}
}
```

- `business_name` (optional, top level; falls back to `brief.user_name`) is who the call is for.
  The bridge strips URLs, keeps only `A-Z a-z 0-9 space & ' . , -` and cuts it to 80
  characters (the same for `brief.user_name`). When a person answers, the carrier's own speech
  plays the fixed disclosure, "Hi, this is an AI assistant calling on behalf of
  {business_name}.", before the voice model is connected. `disclosure_spoken` is true only
  when that playback completed; if it is cut off, the call is hung up. The voice model is told
  the introduction was given and does not repeat it.
- `brief` is the plugin's `voice_payload`: the only data the voice model may ever see.
  `brief.facts` holds consented facts only. The client filters it a second time just before
  sending. The goal is redacted. CVV, passwords and one-time codes are dropped even with
  consent. Raw facts, unconsented fields and the user's stored values never leave the
  workspace.
- **Approval, the third enforcement point.** The workspace is the trust boundary for its own
  approvals: the bridge does not try to prove that a human tapped "call" (there is no signed
  receipt). It does refuse (`403 approval_required`)
  a body with no `approval.id` or no `approval.consumed_at`. It must also refuse a body whose
  `expires_at` had already passed at `consumed_at`. Each `approval.id` is single-use per
  workspace: the first `PUT` consumes it and creates the call.
- **Idempotent by call id.** If the same `call_id` arrives again with the same `approval.id`,
  the bridge returns the existing call (`200`, same body) and does not dial again. If a
  different `approval.id` arrives for an existing `call_id`, or a used `approval.id` arrives for
  another `call_id`, the answer is `409`.
- **Bridge-side safety, from the bridge's own ledger.** The bridge enforces these rules
  itself, using its own per-workspace call ledger, never the plugin's numbers:
  - the emergency and premium-number blocklist;
  - one active call per workspace;
  - the daily cap;
  - the hard maximum duration (it clamps `limits.max_duration_s`);
  - quiet hours (21:00 to 08:00 in the callee's local time);
  - no auto-redial;
  - the AI disclosure as the first sentence.

  The plugin keeps its own copies of these checks (defence in depth), but the bridge never
  trusts them. Breaking a rule gives `422` with a clear `code` (`blocked_number`,
  `active_call`, `daily_cap`, `quiet_hours`).
- `201` (new) or `200` (repeat): `{"call_id": "...", "state": "dialing", "bridge_call_id":
  "<opaque, optional>"}`.

On any error the plugin marks the call `failed` with that message. It does not retry and does
not redial. If the request's outcome is unknown (timeout, connection lost), the plugin also
sends `POST .../hangup` once, best effort, so a call created by a request whose response was
lost cannot stay live.

### `GET /v1/calls/{call_id}/updates?cursor=<opaque>`
The plugin's watcher polls this once a second while the call is live. After a restart, the
`pre_llm_call` hook polls it once per turn. With no `cursor`, the bridge returns everything from
the start.

```json
{
  "cursor": "opaque, at most 512 chars",
  "call": {"state": "in_progress", "outcome": null, "reason": null, "duration_s": null,
           "disclosure_spoken": true, "result": null, "usage": null},
  "events": [{"id": "e41", "kind": "ringing", "ts": 1762268412.1, "data": {}}],
  "questions": [{"id": "q7f3a", "question": "Name for the booking?", "field": "name",
                 "suggested": "Winch", "kind": "question", "consent_field": null,
                 "status": "open"}],
  "transcript": [{"id": "t9", "role": "agent", "text": "Hi, this is an AI assistant ...",
                  "ts": 1762268413.0}]
}
```

- **Idempotent mirroring.** Every event and transcript line has a bridge-assigned `id`
  (`[A-Za-z0-9_-]{1,64}`, unique within its call). The plugin drops items that have no `id`
  and ignores any `id` it has already stored. A page served twice (for example after a crash
  before the plugin saved the cursor) therefore changes nothing.
- `events` and `transcript` hold only what is new since `cursor`. `questions` holds the full
  current form of every question created or changed since `cursor`. `call` is always the
  current snapshot.
- `call.state` must be one of `dialing`, `ringing`, `in_progress`, `on_hold_for_user`,
  `voicemail`, `completed`, `no_answer`, `busy`, `failed`, `aborted`. Any other value is
  ignored, so the bridge cannot move a call back to approved. The plugin mirrors the state; it
  may skip intermediate local states. Once a call is terminal locally, it stays that way.
- `call.result` (on end) is the `end_call` result:
  `{"outcome", "reason", "summary", "agreed": {...}, "next_steps"}`. It is stored once.
- **Cost: `call.usage`.** Send `{"cost_usd": 0.42}` once the bridge knows what the call cost
  the workspace (the figure counted against its allowance). In remote mode this is the only
  cost the plugin ever shows. It drops its own estimate from the approval card and the result,
  and when `usage` is absent or invalid it shows no cost figure at all. The latest valid value
  wins.
- A question `id` must match `[A-Za-z0-9_-]{1,64}` and be unique within the workspace. A
  question `status` is `open`, `answered`, `declined` or `timeout`; the plugin acts only on
  `timeout`. `kind` is `question` or `consent`.
- Transcript `role` is `agent` or `callee`. When `options.store_transcripts` was false, the
  bridge sends none, and the plugin drops any it receives anyway.
- Every string from the bridge is untrusted callee data. The plugin shows it as quoted text and
  never treats it as instructions.
- `404` or `401`/`403` here means the bridge no longer knows this call for this token, and the
  plugin fails the call locally (`bridge_rejected`). For other errors the plugin keeps polling,
  and fails the call locally (`bridge_unreachable`) after 120 s without a good response.

### `POST /v1/calls/{call_id}/questions/{question_id}/answer`
```json
{"status": "answered", "answer": "Winch"}
{"status": "declined"}
```
Each answer is sent once. `200` means accepted. `409` means the question was already closed
(timed out, the call ended), and the plugin treats it as delivered. Before sending, the client
checks again: if the question asks for a CVV, password or one-time code, or if the answer holds
a sensitive value the user did not consent to for this call, the plugin sends
`{"status": "declined"}` instead and the value stays in the workspace.

### `POST /v1/calls/{call_id}/hangup`
Body `{}`. The user's kill switch (`/phone hangup`). It is idempotent: `200` when the call is
ending or has ended, `409` when it was already over (the plugin treats this as success too). The
terminal state then comes back through `updates`.

## Running the shared bridge

The deployed bridge is one uvicorn app behind a reverse proxy at `/bridge/phone`:

```
uvicorn --factory hermes_phone.bridge.shared:create_app --host 127.0.0.1 --port 8790
```

It answers with and without the `/bridge/phone` prefix. Install with the `bridge` extra
(`hermes-phone[bridge]`), which adds `cryptography` for webhook signatures; the plugin install
does not need it. It writes only to its SQLite file (default `/data/phone-bridge.db`) and `/tmp`,
so it runs with a read-only root filesystem as an unprivileged user.

Secrets are read only from files in `$CREDENTIALS_DIRECTORY`, never from plain env:
`openai_api_key`, `telnyx_api_key`, `telnyx_public_key` (base64 Ed25519 webhook key) and
`bridge_signing_key`. A missing file is logged by name; `/healthz` stays up and new calls get
`503 not_configured`.

Run one process: at startup it hangs up and fails every call an earlier process left live
(`bridge_restart`). Every 30 seconds it does the same for calls older than their duration cap
plus ring time plus 30 seconds (`stale_call`), and when a media stream drops it hangs up at the
carrier. Once a day it deletes events and media tokens older than 30 days, and transcripts
older than their retention.

| Env | Meaning |
|---|---|
| `HERMES_PHONE_BRIDGE_PUBLIC_URL` (alias `PUBLIC_BASE_URL`) | Public base URL including the prefix, used for webhook and media URLs. Required. |
| `TELNYX_CONNECTION_ID`, `HERMES_PHONE_CALLER_NUMBER` | Outbound connection and caller id. Required. |
| `HERMES_PHONE_BRIDGE_AUTH_URL` | Usage-token check URL. Unset turns the usage-token path off. |
| `HERMES_PHONE_BRIDGE_OVERRIDES` | Optional JSON object, or a path to one: `{"<workspace>": {"daily_cap": 10, "timezone": "America/New_York", "disabled": true}}`. Defaults are 10 calls a day and America/New_York. A bad file refuses new calls. |
| `HERMES_PHONE_ALLOW_ALL` | Off by default, so only allowlisted test numbers can be dialed. Production sets it to `1`. |
| `HERMES_PHONE_BRIDGE_INBOUND` | `1` mounts the inbound callback stub (`POST /v1/telnyx/inbound`). Off by default; otherwise the bridge handles only webhooks for calls it placed. |
| `HERMES_PHONE_BRIDGE_TRUST_PROXY` | On by default: the client address for rate limits is the first `X-Forwarded-For` entry, which the gate overwrites with one real address. The bridge listens on loopback only. `0` uses the socket address. |
| `HERMES_PHONE_STORE_TRANSCRIPTS` | `0` stores none, `1` (default) keeps them 30 days, a number `N` of 2 or more keeps them `N` days. |
| `HERMES_PHONE_DB_PATH`, `HERMES_PHONE_MODEL`, `HERMES_PHONE_VOICE`, `HERMES_PHONE_TELEPHONY_USD_PER_MIN`, `HERMES_PHONE_VOICE_USD_PER_MIN` | Optional tuning. |

Routes, all also under `/bridge/phone`: `GET /healthz`, `GET /v1/health`,
`PUT /v1/calls/{call_id}`, `GET /v1/calls/{call_id}/updates`,
`POST /v1/calls/{call_id}/questions/{question_id}/answer`, `POST /v1/calls/{call_id}/hangup`,
`POST /v1/telnyx/calls/{internal_id}/{secret}` (signed call webhooks),
`WS /v1/media/{internal_id}?token=<one-time>` (call audio), and the optional inbound stub.

## Versioning

The version is the path prefix `/v1`. Adding fields is not a breaking change, and both sides
ignore fields they do not know. Removing or renaming fields, or changing what they mean,
requires `/v2`.
