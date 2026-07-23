import asyncio
from collections import deque
from typing import Any

from watch2.probe import DialogDetected, DialogGone, DialogProbe, ProbeError


def runner_for(*responses: tuple[int, str, str]) -> Any:
    pending = deque(responses)

    async def runner(_argv: list[str]) -> tuple[int, str, str]:
        return pending.popleft()

    return runner


async def test_detects_marker_and_trims_excerpt() -> None:
    screen = "\n".join(
        [f"line {index}" for index in range(30)]
        + ["  ❯ 1. Approve", "    2. Reject"]
    )
    queue: asyncio.Queue[Any] = asyncio.Queue()
    probe = DialogProbe(runner_for((0, screen, "")), "work", queue)
    await probe.poll_once()
    event = await queue.get()
    assert isinstance(event, DialogDetected)
    assert "❯ 1. Approve" in event.screen_excerpt
    assert "2. Reject" in event.screen_excerpt
    # カーソル行の 8 行前から抜粋する（それより古いスクロールバックは捨てる）
    lines = event.screen_excerpt.splitlines()
    assert lines[0] == "line 22"
    assert len(lines) == 10


async def test_non_dialog_is_silent() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    probe = DialogProbe(
        runner_for((0, "ordinary output\n❯ prompt", "")), "work", queue
    )
    await probe.poll_once()
    assert queue.empty()


async def test_duplicate_navigation_and_gone() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    probe = DialogProbe(
        runner_for(
            (0, "❯ 1. Yes", ""),
            (0, "❯ 3. Chat", ""),
            (0, "normal", ""),
        ),
        "work",
        queue,
    )
    await probe.poll_once()
    await probe.poll_once()
    assert isinstance(await queue.get(), DialogDetected)
    assert queue.empty()
    await probe.poll_once()
    assert isinstance(await queue.get(), DialogGone)


async def test_error_once_until_recovery() -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    probe = DialogProbe(
        runner_for(
            (1, "", "pane missing"),
            (1, "", "pane missing"),
            (0, "normal", ""),
            (1, "", "gone again"),
        ),
        "work",
        queue,
    )
    await probe.poll_once()
    await probe.poll_once()
    assert isinstance(await queue.get(), ProbeError)
    assert queue.empty()
    await probe.poll_once()
    await probe.poll_once()
    event = await queue.get()
    assert isinstance(event, ProbeError)
    assert "gone again" in event.reason
