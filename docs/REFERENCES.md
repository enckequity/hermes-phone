# REFERENCES

Verified (opened while writing this pack, Nov 2026):
- Hermes plugin developer guide (manifest, register, tools, hooks, slash commands, ask_choice, storage, doctor, validate): https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- Hermes plugins user guide (post_message / apost_message, live editable messages, contextvars binding): https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
- Hermes catalog submission: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission
- Hermes repo and plugin-catalog: https://github.com/NousResearch/hermes-agent (see plugin-catalog/README.md and plugin-catalog/shipframe.yaml)
- send_message is not a model tool by design: https://github.com/NousResearch/hermes-agent/blob/main/tools/send_message_tool.py
- Twilio Media Streams websocket messages: https://www.twilio.com/docs/voice/media-streams/websocket-messages
- Twilio + OpenAI Realtime outbound calls (Python): https://www.twilio.com/en-us/blog/outbound-calls-python-openai-realtime-api-voice
- OpenAI Realtime guide: https://platform.openai.com/docs/guides/realtime
- OpenAI Realtime conversations: https://developers.openai.com/api/docs/guides/realtime-conversations
- G.711 mu-law format for GA Realtime (`audio/pcmu`): https://community.openai.com/t/gpt-realtime-2-ga-api-what-is-the-correct-audio-format-for-g711-ulaw-twilio-telephony/1380750
- Gemini Live API: https://ai.google.dev/gemini-api/docs/live-api and https://ai.google.dev/gemini-api/docs/live-api/capabilities
- Pipecat Twilio websockets (dial-out): https://docs.pipecat.ai/guides/telephony/twilio-websockets

P2 verification (Nov 2026). Network note: in the build sandbox `developers.openai.com` and
`www.twilio.com` did not resolve (not in the allowed domains), so the pages could not be opened.
Facts below are marked by how they were established.

Verified by web search result text (Twilio docs quoted by the search tool):
- Twilio `<Stream url>` does NOT support query string parameters (error 31920 lists them as a cause
  of a failed websocket handshake). Custom values go in nested `<Parameter name= value=/>` and arrive
  in the websocket `start` message as `start.customParameters`. The bridge therefore passes its
  one-time token as `<Parameter name="token">` and validates it from `customParameters`.
- OpenAI GA Realtime session.update is `{"type":"session.update","session":{"type":"realtime",
  "model":..., "output_modalities":["audio"], "audio":{"input":{...},"output":{...}}, ...}}`, the
  example model is `gpt-realtime-2.1`, turn detection is `server_vad` or `semantic_vad`, the voice
  cannot change after the first audio response. Endpoint `wss://api.openai.com/v1/realtime?model=<id>`
  with `Authorization: Bearer`. The GA API must not send the old `OpenAI-Beta: realtime=v1` header.
- `audio/pcmu` as the G.711 mu-law format value was only seen in OpenAI-compatible third-party docs
  plus the community thread listed above; the bridge uses it and the model id/voice are config.

Verified from the installed SDK (`twilio` 9.12, introspected locally, not network):
- `client.calls.create(to, from_, url, method, status_callback, status_callback_event,
  status_callback_method, machine_detection, async_amd, async_amd_status_callback,
  async_amd_status_callback_method, time_limit, timeout, ...)`; `client.calls(sid).update(status=,
  twiml=, url=, ...)`. `async_amd` is a string ("true").
- `twilio.request_validator.RequestValidator(token).validate(uri, params, signature)` and
  `.compute_signature(uri, params)`. The signature covers the exact public URL plus sorted POST
  params, so behind a tunnel or Railway proxy the bridge validates against
  `PUBLIC_BASE_URL + path (+ ?query)`, never the internal `Host`.

From memory of the Twilio docs (UNVERIFIED this session, coded defensively):
- Media Streams messages: inbound `connected`, `start` (`streamSid`, `callSid`, `mediaFormat`
  {encoding `audio/x-mulaw`, sampleRate 8000, channels 1}, `customParameters`), `media`
  (`track`, `chunk`, `timestamp` in ms, base64 `payload`), `mark` (`mark.name`), `stop`; outbound
  `media`, `mark`, `clear` (all with `streamSid`). The bridge ignores unknown events.
- Status callback fields `CallSid`, `CallStatus` (queued, initiated, ringing, in-progress, completed,
  busy, no-answer, failed, canceled), `CallDuration` (seconds, on completed), `AnsweredBy`
  (human, machine_start, machine_end_beep, machine_end_silence, machine_end_other, fax, unknown).
  With `async_amd` the AnsweredBy arrives in a separate callback to `async_amd_status_callback`
  (no CallStatus); the bridge routes both to `/status/{id}` and handles either shape.
- OpenAI Realtime GA event names used (from the pack's docs list, not re-opened): client
  `session.update`, `input_audio_buffer.append`, `response.create` (with `response.instructions`),
  `response.cancel`, `conversation.item.truncate` (`item_id`, `content_index`, `audio_end_ms`),
  `conversation.item.create` with `function_call_output`; server `response.created`,
  `response.output_audio.delta` (`item_id`, `delta`), `response.output_audio_transcript.done`,
  `conversation.item.input_audio_transcription.completed`, `input_audio_buffer.speech_started`,
  `response.function_call_arguments.done` (`name`, `call_id`, `arguments`), `response.done`, `error`.
  Input transcription config uses `audio.input.transcription.model` (`gpt-4o-mini-transcribe`).
  Re-check against live docs before the real-call demo; first thing to try if audio is silent.

UNVERIFIED (not opened; check before relying):
- Hermes cron docs (for the retry offer): https://hermes-agent.nousresearch.com/docs/user-guide/features/cron
- Hermes Telegram gateway behavior for ask_choice buttons and post_message edits.
- Whether ctx.ask_choice works inside a tool handler (docs say sync slash handlers only; design does not rely on it).
- Whether `{"action":"approve"}` from pre_tool_call fails closed in cron and gateway contexts.
- Exact name of the Hermes memory tool usable via ctx.dispatch_tool for suggestions.
- FCC Feb 2024 declaratory ruling on AI voices under TCPA.
