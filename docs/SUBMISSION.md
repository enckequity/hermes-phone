# hermes-phone SUBMISSION

Source: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission and `plugin-catalog/README.md` in NousResearch/hermes-agent.

## Shape
- Code lives in your own public repo (third-party SaaS integrations go standalone, not into core).
- The PR to NousResearch/hermes-agent adds exactly one file: `plugin-catalog/hermes-phone.yaml`.
- Pin `sha` to a full 40-character commit SHA (no tags, no branches). Quote `version`.
- Only the repo owner/maintainer submits. Disclose capabilities in the PR. Security scan runs on the pinned commit.

## Catalog YAML template (model fields on an existing entry such as shipframe.yaml before submitting)
```yaml
name: hermes-phone
title: Hermes Phone
repo: https://github.com/winchxyz/hermes-phone
sha: "<40-char commit sha>"
version: "0.1.0"
description: |
  Let Hermes place phone calls for you with explicit approval. Shows a brief card,
  calls only after you tap Call, discloses it is an AI, relays mid-call questions to
  you with a suggested answer, and reports the result and transcript.

  Disclosure: places outbound PSTN calls via your Twilio account (costs money),
  streams call audio to OpenAI Realtime, runs a local FastAPI server exposed via
  your tunnel (PUBLIC_BASE_URL), stores call logs and transcripts in plugin data.
  Recording is off by default. Emergency and premium numbers are blocked.
maintainer: winchxyz
tier: community
category: voice
docs_url: https://github.com/winchxyz/hermes-phone#readme
platforms: []
capabilities:
  provides_tools: [phone_prepare, phone_call, phone_status, phone_transcript]
  provides_hooks: [pre_tool_call, pre_llm_call]
  provides_middleware: []
  requires_env:
    - TWILIO_ACCOUNT_SID
    - TWILIO_AUTH_TOKEN
    - TWILIO_FROM_NUMBER
    - OPENAI_API_KEY
    - PUBLIC_BASE_URL
```

## PR checklist
- [ ] `hermes plugins validate /path/to/hermes-phone --install-deps` passes.
- [ ] `hermes plugins doctor /path/to/hermes-phone --ci` passes (no network in register, tools match manifest).
- [ ] provides_tools / provides_hooks in plugin.yaml exactly match what `register()` registers.
- [ ] All python_dependencies have upper bounds.
- [ ] No self-update, no core patching, no reading credential files.
- [ ] README: setup (Twilio, tunnel, env), safety model, demo video link, costs, legal notes.
- [ ] LICENSE (MIT).
- [ ] PR body: what it does, capability disclosure (network, money, audio to third party, local server), demo video, test summary, hackathon tag.
- [ ] Demo video 60 to 90 s uploaded (YouTube unlisted or X post) and linked.
- [ ] SHA in YAML equals the commit you tested.

## Legal and policy notes for README (not legal advice)
- AI disclosure at the start of every call. Some jurisdictions require it; it is also the honest default.
- Recording consent: some places require all-party consent. Recording is opt-in and announced.
- US TCPA/FCC: AI-generated voices in calls are treated as "artificial voice" (FCC ruling Feb 2024, UNVERIFIED citation link). This plugin only makes user-initiated, one-off calls to businesses the user chose, never bulk or marketing calls.
- No emergency numbers. No calls to people who did not expect them for sales. Rate limits enforced.
- Twilio trial accounts cannot run custom call instructions (they play a trial message), and upgraded accounts need an approved Trust Hub customer profile (KYC). Web phone mode needs no Twilio account and is the easiest way to try the plugin.
- Valid catalog categories: desktop, memory, platform, web, tools, voice, automation, models, general.
