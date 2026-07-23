from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from watch2.bot import ChannelState, MESSAGE_CHUNK_SIZE, NORMAL_STATE, StateKind, WatchClient
from watch2.config import ProjectConfig
from watch2.probe import DialogDetected, DialogGone, ProbeError
from watch2.tail import (
    ApprovalOption,
    ApprovalQuestion,
    ApprovalRequest,
    ApprovalResolved,
    AssistantTurn,
    UserPrompt,
)


def make_client() -> tuple[WatchClient, MagicMock]:
    client = WatchClient.__new__(WatchClient)
    client.projects = {
        10: ProjectConfig("work:0.0", Path("/srv/work"))
    }
    client.runner = AsyncMock()
    client.states = {10: NORMAL_STATE}
    client.recent_sent = {10: deque(maxlen=5)}
    channel = MagicMock()
    channel.send = AsyncMock()
    client.get_channel = MagicMock(return_value=channel)
    return client, channel


def approval_request() -> ApprovalRequest:
    return ApprovalRequest(
        "tool-1",
        (
            ApprovalQuestion(
                "どちら？",
                "確認",
                False,
                (ApprovalOption("A", "一つ目"),),
            ),
        ),
    )


async def test_jsonl_state_transitions() -> None:
    client, channel = make_client()
    await client.handle_event(10, approval_request())
    assert client.states[10] == ChannelState(
        StateKind.AWAITING, "jsonl:tool-1"
    )
    assert "1️⃣ A — 一つ目" in channel.send.await_args.args[0]

    await client.handle_event(
        10, ApprovalResolved("tool-1", {"どちら？": "A"})
    )
    assert client.states[10] == NORMAL_STATE
    assert channel.send.await_args.args[0] == "✅ 回答: どちら？ → A"


async def test_pane_state_transitions() -> None:
    client, channel = make_client()
    await client.handle_event(10, DialogDetected("❯ 1. Approve"))
    assert client.states[10] == ChannelState(StateKind.AWAITING, "pane")
    assert "番号で返信できます" in channel.send.await_args.args[0]
    await client.handle_event(10, DialogGone())
    assert client.states[10] == NORMAL_STATE
    assert channel.send.await_args.args[0] == "✅ ダイアログ解決"


async def test_dialog_ignored_during_jsonl_awaiting() -> None:
    client, channel = make_client()
    client.states[10] = ChannelState(StateKind.AWAITING, "jsonl:tool-1")
    await client.handle_event(10, DialogDetected("❯ 1. A"))
    channel.send.assert_not_awaited()


async def test_number_reply_sends_digit_without_enter() -> None:
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        if "#{pane_id}" in argv:
            return 0, "%1\n", ""
        if "#{pane_index}\t#{pane_current_path}" in argv:
            return 0, "0\t/srv/work\n", ""
        return 0, "", ""

    client, _channel = make_client()
    client.runner = runner
    client.states[10] = ChannelState(StateKind.AWAITING, "pane")
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        channel=SimpleNamespace(id=10),
        content="3",
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    await client.on_message(message)
    assert calls[-1] == ["tmux", "send-keys", "-t", "work:0.0", "3"]
    assert all("Enter" not in call for call in calls)


async def test_non_digit_guard_and_reset() -> None:
    client, _channel = make_client()
    client.states[10] = ChannelState(StateKind.AWAITING, "pane")
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        channel=SimpleNamespace(id=10),
        content="hello",
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    await client.on_message(message)
    message.reply.assert_awaited_once_with(
        "⚠️ 承認の回答待ちです（番号で返信）。解除は !reset"
    )
    message.content = "!reset"
    message.reply.reset_mock()
    await client.on_message(message)
    assert client.states[10] == NORMAL_STATE


async def test_prompt_and_mirror_suppression() -> None:
    async def runner(argv: list[str]) -> tuple[int, str, str]:
        if "#{pane_id}" in argv:
            return 0, "%1\n", ""
        if "#{pane_index}\t#{pane_current_path}" in argv:
            return 0, "0\t/srv/work\n", ""
        return 0, "", ""

    client, channel = make_client()
    client.runner = runner
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False),
        channel=SimpleNamespace(id=10),
        content="hello",
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )
    await client.on_message(message)
    message.add_reaction.assert_awaited_once_with("✅")
    await client.handle_event(10, UserPrompt("hello"))
    channel.send.assert_not_awaited()
    await client.handle_event(10, UserPrompt("terminal"))
    channel.send.assert_awaited_once_with("🖥️ terminal")


async def test_chunking_and_warning_format() -> None:
    client, channel = make_client()
    text = "x" * (MESSAGE_CHUNK_SIZE + 7)
    await client.handle_event(10, AssistantTurn(text))
    assert [call.args[0] for call in channel.send.await_args_list] == [
        "x" * MESSAGE_CHUNK_SIZE,
        "x" * 7,
    ]
    channel.send.reset_mock()
    await client.handle_event(10, ProbeError("pane\nmissing"))
    assert channel.send.await_args.args[0] == "⚠️ pane missing"
