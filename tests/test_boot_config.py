"""deploy/boot-config.py merges env into config.yaml (hermes_yaml stubbed with PyYAML)."""
import importlib.util
import sys
import types
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "boot-config.py"


def _load():
    stub = types.ModuleType("hermes_yaml")
    stub.safe_load = lambda text: yaml.safe_load(text)
    stub.safe_dump = lambda data, sort_keys=True: yaml.safe_dump(data, sort_keys=sort_keys)
    sys.modules["hermes_yaml"] = stub
    spec = importlib.util.spec_from_file_location("boot_config", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_boot_config_merges(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled: [other]\nmodel: gpt-x\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_PHONE_USER_NAME", "Pat")
    monkeypatch.setenv("HERMES_PHONE_CALLBACK_NUMBER", "+15555550100")
    monkeypatch.setenv("HERMES_PHONE_DAILY_CALL_LIMIT", "3")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    assert _load().main() == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["plugins"]["enabled"] == ["other", "hermes-phone"]
    settings = cfg["plugins"]["entries"]["hermes-phone"]["settings"]
    assert settings == {"user_name": "Pat", "callback_number": "+15555550100",
                        "daily_call_limit": 3}
    assert cfg["model"] == {"default": "gpt-x", "provider": "openrouter"}
    assert _load().main() == 0  # idempotent
    again = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert again["plugins"]["enabled"].count("hermes-phone") == 1


def test_boot_config_without_existing_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k in ("HERMES_PHONE_USER_NAME", "HERMES_MODEL", "HERMES_INFERENCE_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    assert _load().main() == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["plugins"]["enabled"] == ["hermes-phone"]
    assert "model" not in cfg


def test_gemini_default_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k in ("HERMES_MODEL", "HERMES_INFERENCE_PROVIDER", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert _load().main() == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["model"] == {"provider": "gemini", "default": "gemini-3.8-flash"}
    monkeypatch.setenv("HERMES_PHONE_LLM_MODEL", "gemini-3.7-flash")
    assert _load().main() == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["model"]["default"] == "gemini-3.7-flash"


def test_explicit_provider_wins_over_gemini_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "openrouter")
    monkeypatch.setenv("HERMES_PHONE_LLM_MODEL", "z-ai/glm-5.2:free")
    monkeypatch.setenv("HERMES_PHONE_FREE_ONLY", "1")
    assert _load().main() == 0
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert cfg["model"] == {"provider": "openrouter", "default": "z-ai/glm-5.2:free"}
    assert cfg["auxiliary"]["free_only"] is True


def test_gemini_replaces_auto_provider_and_foreign_default(tmp_path, monkeypatch):
    """Regression: the image seeds provider=auto + a Claude model; with OPENAI_API_KEY set,
    auto picked OpenAI and failed with 404. A Gemini key must take over both fields."""
    home = tmp_path
    (home / "config.yaml").write_text(
        "model:\n  provider: auto\n  default: anthropic/claude-opus-4.6\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    for name in ("HERMES_INFERENCE_PROVIDER", "HERMES_MODEL", "HERMES_PHONE_LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert _load().main() == 0
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    assert cfg["model"]["provider"] == "gemini"
    assert cfg["model"]["default"].startswith("gemini")
