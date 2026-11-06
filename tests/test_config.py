from pathlib import Path

import pytest
from pydantic import ValidationError

from swarm.config import LiveCallRefused, ModelConfig, RunConfig, Settings, load_run_config

BASE = {"run_name": "arm-a", "model": {"name": "claude-sonnet-5-5"}, "budget_usd": 2.0, "seed": 7}


def test_defaults():
    run = RunConfig.model_validate(BASE)
    assert run.dry_run is True
    assert run.provider == "anthropic"
    assert run.concurrency == 8
    assert run.cache_ttl == "5m"
    assert run.model == ModelConfig(
        name="claude-sonnet-5-5", max_tokens=1024, temperature=1.0, thinking_budget=None
    )
    assert run.run_dir == Path("runs") / "arm-a"


def test_live_run_is_refused_without_swarm_live():
    with pytest.raises(LiveCallRefused, match="SWARM_LIVE"):
        RunConfig.model_validate(BASE | {"dry_run": False})


def test_swarm_live_zero_still_refuses(monkeypatch):
    monkeypatch.setenv("SWARM_LIVE", "0")
    with pytest.raises(LiveCallRefused):
        RunConfig.model_validate(BASE | {"dry_run": False})


def test_live_run_is_allowed_with_swarm_live(monkeypatch):
    monkeypatch.setenv("SWARM_LIVE", "1")
    assert RunConfig.model_validate(BASE | {"dry_run": False}).dry_run is False


def test_shell_variable_beats_dotenv(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("SWARM_LIVE=1\n")  # tests run from tmp_path
    monkeypatch.setenv("SWARM_LIVE", "0")
    with pytest.raises(LiveCallRefused):
        RunConfig.model_validate(BASE | {"dry_run": False})
    monkeypatch.delenv("SWARM_LIVE")
    assert Settings().SWARM_LIVE is True  # with no shell variable, the .env value counts


def test_blank_api_key_counts_as_missing(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=\n")
    assert Settings().anthropic_api_key() is None


def test_api_key_is_read_from_dotenv_and_never_shown(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test-123\n")
    settings = Settings()
    assert settings.anthropic_api_key() == "sk-test-123"
    assert "sk-test-123" not in repr(settings)


@pytest.mark.parametrize(("field", "value"), [("temperature", 0.7), ("thinking_budget", 2048)])
def test_v1_model_fields_are_locked(field, value):
    with pytest.raises(ValidationError, match=field):
        ModelConfig(name="claude-sonnet-5-5", **{field: value})


@pytest.mark.parametrize(
    "change",
    [
        {"surprise": 1},
        {"run_name": "../escape"},
        {"run_name": ".hidden"},
        {"run_name": "a/b"},
        {"budget_usd": 0},
        {"cache_ttl": "10m"},
        {"concurrency": 0},
        {"provider": "agentrouter"},
    ],
)
def test_invalid_run_configs_are_rejected(change):
    with pytest.raises(ValidationError):
        RunConfig.model_validate(BASE | change)


def test_load_run_config_from_yaml(tmp_path):
    path = tmp_path / "arm.yaml"
    path.write_text(
        "run_name: arm-b\n"
        "model:\n"
        "  name: claude-haiku-5-5\n"
        "  max_tokens: 512\n"
        "budget_usd: 3\n"
        "cache_ttl: 1h\n"
        "seed: 1\n"
    )
    run = load_run_config(path)
    assert run.run_name == "arm-b"
    assert run.model.name == "claude-haiku-5-5" and run.model.max_tokens == 512
    assert run.budget_usd == 3.0 and run.cache_ttl == "1h" and run.dry_run is True


def test_missing_budget_comes_from_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "arm.yaml"
    path.write_text("run_name: arm-c\nmodel: {name: claude-haiku-5-5}\nseed: 1\n")
    assert load_run_config(path).budget_usd == 5.0  # the Settings default
    monkeypatch.setenv("SWARM_DEFAULT_BUDGET_USD", "2.5")
    assert load_run_config(path).budget_usd == 2.5


def test_yaml_must_be_a_mapping(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a list\n")
    with pytest.raises(ValueError, match="mapping"):
        load_run_config(path)
