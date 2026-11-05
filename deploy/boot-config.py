"""Boot-time Hermes config for the Railway container. Run as the hermes user by 03-hermes-phone.

Merges into $HERMES_HOME/config.yaml (Hermes' own format, via hermes_yaml):
  * plugins.enabled gets "hermes-phone"
  * plugins.entries.hermes-phone.settings.<key> from HERMES_PHONE_USER_NAME / _CALLBACK_NUMBER
    and the optional tuning vars below (only when set; never overwrites with blanks)
  * model.default / model.provider from HERMES_MODEL / HERMES_INFERENCE_PROVIDER (when set)
  * free-LLM default: with GEMINI_API_KEY (or GOOGLE_API_KEY) set and no provider chosen in
    config.yaml or HERMES_INFERENCE_PROVIDER, model.provider=gemini and model.default=
    HERMES_PHONE_LLM_MODEL or gemini-3.8-flash. HERMES_PHONE_LLM_MODEL overrides model.default for
    any provider (e.g. an OpenRouter ":free" id). HERMES_PHONE_FREE_ONLY=1 sets auxiliary.free_only.
Never touches auth.json or .env, and never prints values.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import hermes_yaml as yaml

PLUGIN_ID = "hermes-phone"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"  # first entry of the gemini catalogue in Hermes
SETTINGS_FROM_ENV = {
    "user_name": ("HERMES_PHONE_USER_NAME", str),
    "callback_number": ("HERMES_PHONE_CALLBACK_NUMBER", str),
    "question_timeout_s": ("HERMES_PHONE_QUESTION_TIMEOUT_S", int),
    "model": ("HERMES_PHONE_MODEL", str),
    "voice": ("HERMES_PHONE_VOICE", str),
    "max_duration_s": ("HERMES_PHONE_MAX_DURATION_S", int),
    "daily_call_limit": ("HERMES_PHONE_DAILY_CALL_LIMIT", int),
}


def _dict(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME") or "/opt/data")
    path = home / "config.yaml"
    config: dict = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
        if loaded is not None and not isinstance(loaded, dict):
            print("boot-config: config.yaml is not a mapping; untouched", file=sys.stderr)
            return 0
        config = loaded or {}

    plugins = _dict(config, "plugins")
    enabled = plugins.get("enabled")
    enabled = list(enabled) if isinstance(enabled, list) else []
    if PLUGIN_ID not in enabled:
        enabled.append(PLUGIN_ID)
    plugins["enabled"] = enabled

    settings = _dict(_dict(_dict(plugins, "entries"), PLUGIN_ID), "settings")
    applied = []
    for key, (env_name, kind) in SETTINGS_FROM_ENV.items():
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        try:
            settings[key] = kind(raw)
        except ValueError:
            print(f"boot-config: {env_name} is not a valid {kind.__name__}", file=sys.stderr)
            continue
        applied.append(key)

    model = config.get("model")
    model = {"default": model} if isinstance(model, str) and model else (
        model if isinstance(model, dict) else {})
    for key, env_name in (("default", "HERMES_MODEL"), ("provider", "HERMES_INFERENCE_PROVIDER")):
        raw = (os.environ.get(env_name) or "").strip()
        if raw:
            model[key] = raw
            applied.append(f"model.{key}")
    env = os.environ
    env_provider = (env.get("HERMES_INFERENCE_PROVIDER") or "").strip()
    # The image seeds config.yaml with provider "auto"/empty and a non-Gemini default model.
    # "auto" then picks OpenAI whenever OPENAI_API_KEY is set (it is: we need it for Realtime),
    # which pairs the OpenAI endpoint with a model OpenAI does not serve. So "auto" counts as
    # unset, and env wins over whatever an earlier boot wrote.
    cfg_provider = str(model.get("provider") or "").strip().lower()
    explicit_provider = env_provider or (cfg_provider if cfg_provider not in ("", "auto") else "")
    gemini_key = (env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()
    if gemini_key and explicit_provider in ("", "gemini"):
        model["provider"] = "gemini"
        if not str(model.get("default") or "").startswith("gemini"):
            model["default"] = GEMINI_DEFAULT_MODEL
        applied.append("model.provider=gemini")
    llm_model = (os.environ.get("HERMES_PHONE_LLM_MODEL") or "").strip()
    if llm_model:
        model["default"] = llm_model
        applied.append("model.default")
    if model:
        config["model"] = model
    if (os.environ.get("HERMES_PHONE_FREE_ONLY") or "").strip().lower() in ("1", "true", "yes"):
        _dict(config, "auxiliary")["free_only"] = True
        applied.append("auxiliary.free_only")

    home.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    tmp.chmod(0o640)
    tmp.replace(path)
    print(f"boot-config: plugins.enabled has {PLUGIN_ID}; applied: {', '.join(applied) or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
