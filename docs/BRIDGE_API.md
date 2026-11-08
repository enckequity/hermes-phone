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
| `HERMES_PHONE_BRIDGE_TOKEN` | Workspace credential, read through the profile-scoped secret lookup. |

Every request sends `Authorization: Bearer <token>`. The bridge works out the workspace from the
token, so no request or body names a workspace. A call id, question id or cursor that belongs to
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

- `brief` is the plugin's `voice_payload`: the only data the voice model may ever see.
  `brief.facts` holds consented facts only. The client filters it a second time just before
  sending. The goal is redacted. CVV, passwords and one-time codes are dropped even with
  consent. Raw facts, unconsented fields and the user's stored values never leave the
  workspace.
- **Approval, the third enforcement point.** The bridge must refuse (`403 approval_required`)
  a body with no `approval.id` or no `approval.consumed_at`. It must also refuse a body whose
  `expires_at` had already passed at `consumed_at`. Each `approval.id` is single-use per
  workspace: the first `PUT` consumes it and creates the call.
- **Idempotent by call id.** If the same `call_id` arrives again with the same `approval.id`,
  the bridge returns the existing call (`200`, same body) and does not dial again. If a
  different `approval.id` arrives for an existing `call_id`, or a used `approval.id` arrives for
  another `call_id`, the answer is `409`.
- The bridge keeps its own safety rules no matter what the plugin sent: the emergency and
  premium-number blocklist, one active call per workspace, the daily cap, the hard
  maximum duration, quiet hours, no auto-redial, and the AI disclosure as the first sentence.
  Breaking one of them gives `422` with a clear `code`.
- `201` (new) or `200` (repeat): `{"call_id": "...", "state": "dialing", "bridge_call_id":
  "<opaque, optional>"}`.

On any error the plugin marks the call `failed` with that message. It does not retry and does
not redial. If the request's outcome is unknown (timeout, connection lost), the plugin also
sends `POST .../hangup` once, best effort, so a call created by a request whose response was
lost cannot stay live.

### `GET /v1/calls/{call_id}/updates?cursor=<opaque>`
The plugin's watcher polls this about every 0.5 s while the call is live. After a restart, the
`pre_llm_call` hook polls it once per turn. With no `cursor`, the bridge returns everything from
the start.

```json
{
  "cursor": "opaque, at most 512 chars",
  "call": {"state": "in_progress", "outcome": null, "reason": null, "duration_s": null,
           "disclosure_spoken": true, "result": null},
  "events": [{"kind": "ringing", "ts": 1762268412.1, "data": {}}],
  "questions": [{"id": "q7f3a", "question": "Name for the booking?", "field": "name",
                 "suggested": "Winch", "kind": "question", "consent_field": null,
                 "status": "open"}],
  "transcript": [{"role": "agent", "text": "Hi, this is an AI assistant ...", "ts": 1762268413.0}]
}
```

- `events` and `transcript` hold only what is new since `cursor`. `questions` holds the full
  current form of every question created or changed since `cursor`. `call` is always the
  current snapshot.
- `call.state` must be one of `dialing`, `ringing`, `in_progress`, `on_hold_for_user`,
  `voicemail`, `completed`, `no_answer`, `busy`, `failed`, `aborted`. Any other value is
  ignored, so the bridge cannot move a call back to approved. The plugin mirrors the state; it
  may skip intermediate local states. Once a call is terminal locally, it stays that way.
- `call.result` (on end) is the `end_call` result:
  `{"outcome", "reason", "summary", "agreed": {...}, "next_steps"}`. It is stored once.
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

## Versioning

The version is the path prefix `/v1`. Adding fields is not a breaking change, and both sides
ignore fields they do not know. Removing or renaming fields, or changing what they mean,
requires `/v2`.
