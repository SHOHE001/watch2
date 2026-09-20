import asyncio
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
    client._input_locks = {}
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


def input_message(channel_id, text):
    return SimpleNamespace(
        author=SimpleNamespace(bot=False), channel=SimpleNamespace(id=channel_id),
        content=text, reply=AsyncMock(), add_reaction=AsyncMock(),
    )


async def test_concurrent_prompts_keep_literal_and_enter_together() -> None:
    client, _ = make_client()
    client._validate_session = AsyncMock()
    calls = []

    async def runner(argv):
        calls.append(argv)
        await asyncio.sleep(0)
        return 0, '', ''

    client.runner = runner
    await asyncio.gather(client.on_message(input_message(10, 'first')),
                         client.on_message(input_message(10, 'second')))
    assert [call[-1] for call in calls] == ['first', 'Enter', 'second', 'Enter']


async def test_channels_sharing_a_target_share_the_same_send_order() -> None:
    client, _ = make_client()
    client.projects[20] = client.projects[10]
    client.states[20] = NORMAL_STATE
    client.recent_sent[20] = deque(maxlen=5)
    client._validate_session = AsyncMock()
    calls = []

    async def runner(argv):
        calls.append(argv[-1])
        await asyncio.sleep(0)
        return 0, '', ''

    client.runner = runner
    await asyncio.gather(client.on_message(input_message(10, 'first')),
                         client.on_message(input_message(20, 'second')))
    assert calls == ['first', 'Enter', 'second', 'Enter']


async def test_failed_send_releases_the_target_for_the_next_message() -> None:
    client, _ = make_client()
    client._validate_session = AsyncMock()
    calls = []

    async def runner(argv):
        calls.append(argv[-1])
        await asyncio.sleep(0)
        return (1, '', 'fixture failure') if argv[-1] == 'first' else (0, '', '')

    client.runner = runner
    first = input_message(10, 'first')
    second = input_message(10, 'second')
    await asyncio.gather(client.on_message(first), client.on_message(second))
    assert calls == ['first', 'second', 'Enter']
    first.reply.assert_awaited_once()
    first.add_reaction.assert_not_awaited()
    second.add_reaction.assert_awaited_once_with('✅')


async def test_different_targets_can_progress_independently() -> None:
    client, _ = make_client()
    client.projects[20] = ProjectConfig('other:0.0', Path('/srv/other'))
    client.states[20] = NORMAL_STATE
    client.recent_sent[20] = deque(maxlen=5)
    client._validate_session = AsyncMock()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = []

    async def runner(argv):
        calls.append(argv[-1])
        if argv[-1] == 'first':
            first_started.set()
            await release_first.wait()
        return 0, '', ''

    client.runner = runner
    first = asyncio.create_task(client.on_message(input_message(10, 'first')))
    try:
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.wait_for(client.on_message(input_message(20, 'second')), 1)
        assert calls == ['first', 'second', 'Enter']
    finally:
        release_first.set()
        await first
    assert calls == ['first', 'second', 'Enter', 'Enter']
