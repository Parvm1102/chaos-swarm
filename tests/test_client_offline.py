"""AnthropicClient, driven offline with a fake SDK object and no sleeping between retries."""

import logging
from typing import Any

import anthropic
import pytest
from pydantic import SecretStr
from tenacity import wait_none

from swarm.config import LiveCallRefused, Settings
from swarm.llm.client import MAX_ATTEMPTS, AnthropicClient, to_llm_response
from swarm.llm.meter import BudgetExceeded, read_usage_lines
from swarm.llm.types import Usage, text_block, user_message

SYSTEM = [text_block("You are a probe.")]
MESSAGES = [user_message("Reply with the single word pong")]
TAGS = {"phase": "test", "agent": "probe", "episode": 0}


def live_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "SWARM_LIVE": True,
        "ANTHROPIC_API_KEY": SecretStr("sk-test-not-a-real-key"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def sdk_message(**overrides: Any) -> anthropic.types.Message:
    data: dict[str, Any] = {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "pong"}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "cache_creation_input_tokens": 1_500,
            "cache_read_input_tokens": 0,
        },
    }
    data.update(overrides)
    return anthropic.types.Message.model_validate(data)


def fake_error(cls: type[Exception]) -> Exception:
    """An SDK error instance without an HTTP response: all the retry logic looks at is its type."""
    error = cls.__new__(cls)
    Exception.__init__(error, f"fake {cls.__name__}")
    return error


class FakeMessages:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeSDK:
    def __init__(self, *outcomes: Any) -> None:
        self.messages = FakeMessages(list(outcomes))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def live_run(make_run, monkeypatch):
    monkeypatch.setenv("SWARM_LIVE", "1")  # needed to build a dry_run=false RunConfig at all
    return make_run(dry_run=False)


def make_client(run, meter, sdk) -> AnthropicClient:
    return AnthropicClient(run, meter, settings=live_settings(), sdk=sdk, retry_wait=wait_none())


async def ask(client: AnthropicClient):
    return await client.complete(
        system=SYSTEM, messages=MESSAGES, tools=None, tool_choice=None, max_tokens=64, tags=TAGS
    )


# Refusing to exist -------------------------------------------------------------------------


def test_refuses_a_dry_run(make_run, make_meter):
    with pytest.raises(LiveCallRefused, match="dry_run"):
        AnthropicClient(make_run(), make_meter(), settings=live_settings(), sdk=FakeSDK())


def test_refuses_without_swarm_live(live_run, make_meter):
    with pytest.raises(LiveCallRefused, match="SWARM_LIVE"):
        AnthropicClient(
            live_run, make_meter(), settings=live_settings(SWARM_LIVE=False), sdk=FakeSDK()
        )


def test_refuses_without_an_api_key(live_run, make_meter):
    with pytest.raises(LiveCallRefused, match="ANTHROPIC_API_KEY"):
        AnthropicClient(
            live_run, make_meter(), settings=live_settings(ANTHROPIC_API_KEY=None), sdk=FakeSDK()
        )


def test_refuses_a_meter_priced_at_another_ttl(live_run, make_meter):
    with pytest.raises(ValueError, match="does not match"):
        AnthropicClient(
            live_run, make_meter(cache_ttl="5m"), settings=live_settings(), sdk=FakeSDK()
        )


# One call, end to end ----------------------------------------------------------------------


async def test_a_call_is_built_sent_converted_and_metered(live_run, make_meter):
    sdk = FakeSDK(sdk_message())
    meter = make_meter()
    async with make_client(live_run, meter, sdk) as client:
        response = await ask(client)
    assert sdk.closed

    (request,) = sdk.messages.requests
    assert request["model"] == "claude-haiku-5-5"
    assert request["thinking"] == {"type": "disabled"}
    assert "temperature" not in request
    assert request["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    assert response.text == "pong"
    assert response.usage.cache_creation_input_tokens == 1_500
    (line,) = read_usage_lines(meter.path)
    # Haiku: 10 x 0.10 + 1,500 x 0.20 (1h write) + 2 x 0.50 = 302 per million
    assert line["usd"] == pytest.approx(0.000302)
    assert (line["phase"], line["agent"], line["episode"]) == ("test", "probe", 0)


async def test_a_different_served_model_is_logged(live_run, make_meter, caplog):
    sdk = FakeSDK(sdk_message(model="claude-sonnet-5-5"))
    with caplog.at_level(logging.WARNING, logger="swarm.llm.client"):
        await ask(make_client(live_run, make_meter(), sdk))
    assert "response says claude-sonnet-5-5" in caplog.text


# Retries -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        anthropic.RateLimitError,  # 429
        anthropic.InternalServerError,  # 500
        anthropic.OverloadedError,  # 529
        anthropic.APIConnectionError,
        anthropic.APITimeoutError,
    ],
)
async def test_transient_errors_are_retried(error, live_run, make_meter, caplog):
    sdk = FakeSDK(fake_error(error), fake_error(error), sdk_message())
    meter = make_meter()
    with caplog.at_level(logging.WARNING, logger="swarm.llm.client"):
        response = await ask(make_client(live_run, meter, sdk))

    assert response.text == "pong"
    assert len(sdk.messages.requests) == 3
    assert len(list(read_usage_lines(meter.path))) == 1  # only the successful call is billed
    retries = [record.getMessage() for record in caplog.records if "retrying" in record.message]
    assert len(retries) == 2
    assert error.__name__ in retries[0] and "agent=probe" in retries[0]


@pytest.mark.parametrize(
    "error",
    [
        anthropic.BadRequestError,  # 400
        anthropic.AuthenticationError,  # 401
        anthropic.PermissionDeniedError,  # 403
        anthropic.NotFoundError,  # 404
    ],
)
async def test_client_errors_are_never_retried(error, live_run, make_meter):
    sdk = FakeSDK(fake_error(error), sdk_message())
    meter = make_meter()
    with pytest.raises(error):
        await ask(make_client(live_run, meter, sdk))
    assert len(sdk.messages.requests) == 1
    assert not meter.path.exists()  # nothing billed, nothing logged


async def test_gives_up_after_max_attempts(live_run, make_meter):
    sdk = FakeSDK(*(fake_error(anthropic.RateLimitError) for _ in range(MAX_ATTEMPTS + 1)))
    with pytest.raises(anthropic.RateLimitError):
        await ask(make_client(live_run, make_meter(), sdk))
    assert len(sdk.messages.requests) == MAX_ATTEMPTS == 8


# Budget ------------------------------------------------------------------------------------


async def test_no_call_starts_once_the_run_is_over_budget(live_run, make_meter):
    meter = make_meter(budget_usd=0.01)
    over = Usage(output_tokens=2_000)  # Sonnet: 2,000 x 10 per million = $0.02
    with pytest.raises(BudgetExceeded):
        await meter.record("claude-sonnet-5-5", over, phase="p", agent="a", episode=0)
    sdk = FakeSDK(sdk_message())
    with pytest.raises(BudgetExceeded):
        await ask(make_client(live_run, meter, sdk))
    assert sdk.messages.requests == []


# SDK response -> plain types ---------------------------------------------------------------


def test_to_llm_response_keeps_every_block_exactly():
    blocks = [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Let me add."},
        {
            "type": "tool_use",
            "id": "toolu_9",
            "name": "add",
            "input": {"a": 1, "b": None, "nested": {"k": [1, 2]}},
        },
    ]
    message = sdk_message(
        content=blocks, stop_reason="tool_use", usage={"input_tokens": 5, "output_tokens": 7}
    )
    response = to_llm_response(message)

    assert response.content == blocks  # thinking signature and null inputs survive unchanged
    assert response.text == "Let me add."
    assert response.tool_calls[0].id == "toolu_9"
    assert response.tool_calls[0].input == {"a": 1, "b": None, "nested": {"k": [1, 2]}}
    assert response.stop_reason == "tool_use"
    assert response.usage == Usage(input_tokens=5, output_tokens=7)  # absent cache counts are 0
    assert response.raw["id"] == "msg_test"
