"""The per-provider pacer and the strict flag on tool definitions."""

import pytest

from swarm.llm.client import PACING_S, Pacer
from swarm.llm.types import ToolSpec


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_request_starts_are_spaced_by_the_interval():
    clock = FakeClock()
    pacer = Pacer(2.5, clock=clock, sleep=clock.sleep)
    starts = []
    for _ in range(3):
        await pacer.wait()
        starts.append(clock.now)
    assert starts == [100.0, 102.5, 105.0]


async def test_no_waiting_once_the_interval_has_passed():
    clock = FakeClock()
    pacer = Pacer(2.5, clock=clock, sleep=clock.sleep)
    await pacer.wait()
    clock.now += 10.0
    await pacer.wait()
    assert clock.now == 110.0


async def test_a_zero_interval_never_waits():
    clock = FakeClock()
    pacer = Pacer(0.0, clock=clock, sleep=clock.sleep)
    for _ in range(5):
        await pacer.wait()
    assert clock.now == 100.0


def test_anthropic_needs_no_spacing_and_negative_intervals_are_refused():
    assert PACING_S["anthropic"] == 0.0
    with pytest.raises(ValueError):
        Pacer(-1.0)


def test_strict_is_sent_only_when_set():
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    assert "strict" not in ToolSpec(name="a", description="A.", input_schema=schema).to_anthropic()
    strict = ToolSpec(name="b", description="B.", input_schema=schema, strict=True).to_anthropic()
    assert strict["strict"] is True
