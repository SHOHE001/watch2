import asyncio
import json
from pathlib import Path
from typing import Any

from watch2.tail import (
    ApprovalRequest,
    ApprovalResolved,
    AssistantTurn,
    SessionTailer,
    UserPrompt,
)


def append_rows(path: Path, *rows: dict[str, Any]) -> None:
    with path.open("ab") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False).encode() + b"\n")


def assistant(
    content: list[dict[str, Any]], stop_reason: str | None = None
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": content, "stop_reason": stop_reason},
    }


def make_tailer(
    path: Path,
) -> tuple[SessionTailer, asyncio.Queue[Any]]:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    tailer = SessionTailer(path.parent, queue, poll_interval=0.01)
    tailer._path = path
    return tailer, queue


async def test_accumulates_text_until_end_turn(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    tailer, queue = make_tailer(path)
    append_rows(
        path,
        assistant([{"type": "text", "text": "前半"}]),
        assistant([{"type": "text", "text": "後半"}], "end_turn"),
    )
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("前半\n\n後半")


async def test_tool_use_between_text_is_excluded(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    tailer, queue = make_tailer(path)
    append_rows(
        path,
        assistant(
            [
                {"type": "text", "text": "before"},
                {"type": "tool_use", "id": "x", "name": "Read"},
            ]
        ),
        assistant([{"type": "text", "text": "after"}], "end_turn"),
    )
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("before\n\nafter")


async def test_ask_user_question_and_resolution(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    tailer, queue = make_tailer(path)
    append_rows(
        path,
        assistant(
            [
                {"type": "text", "text": "選択"},
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": "どちら？",
                                "header": "確認",
                                "multiSelect": False,
                                "options": [
                                    {"label": "A", "description": "一つ目"}
                                ],
                            }
                        ]
                    },
                },
            ],
            "tool_use",
        ),
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1"}
                ]
            },
            "toolUseResult": {"answers": {"どちら？": "A"}},
        },
    )
    await tailer.poll_once()

    # 同一バッチで解決済みの要求は配信されない（回答後にまとめて
    # 書かれた AUQ を「いま入力待ち」と誤認しないため）
    assert await queue.get() == AssistantTurn("選択")
    assert await queue.get() == ApprovalResolved(
        "tool-1", {"どちら？": "A"}
    )
    assert queue.empty()


async def test_separate_polls_emit_request_then_resolution(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    tailer, queue = make_tailer(path)
    append_rows(
        path,
        assistant(
            [
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": "どちら？",
                                "header": "確認",
                                "multiSelect": False,
                                "options": [
                                    {"label": "A", "description": "一つ目"}
                                ],
                            }
                        ]
                    },
                },
            ],
            "tool_use",
        ),
    )
    await tailer.poll_once()
    request = await queue.get()
    assert isinstance(request, ApprovalRequest)
    assert request.tool_use_id == "tool-1"
    assert request.questions[0].options[0].label == "A"

    append_rows(
        path,
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1"}
                ]
            },
            "toolUseResult": {"answers": {"どちら？": "A"}},
        },
    )
    await tailer.poll_once()
    assert await queue.get() == ApprovalResolved(
        "tool-1", {"どちら？": "A"}
    )


async def test_skips_sidechain_metadata_and_tool_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    tailer, queue = make_tailer(path)
    append_rows(
        path,
        {"type": "mode", "value": "plan"},
        {
            "type": "user",
            "isSidechain": True,
            "message": {"content": "hidden"},
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "unknown"}
                ]
            },
        },
        {"type": "user", "message": {"content": "terminal prompt"}},
    )
    await tailer.poll_once()
    assert await queue.get() == UserPrompt("terminal prompt")
    assert queue.empty()


async def test_incomplete_line_carry(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.touch()
    tailer, queue = make_tailer(path)
    encoded = json.dumps(
        assistant([{"type": "text", "text": "complete"}], "end_turn")
    ).encode()
    midpoint = len(encoded) // 2
    with path.open("ab") as file:
        file.write(encoded[:midpoint])
    await tailer.poll_once()
    assert queue.empty()

    with path.open("ab") as file:
        file.write(encoded[midpoint:] + b"\n")
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("complete")


async def test_starts_at_end_of_file(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    append_rows(path, assistant([{"type": "text", "text": "old"}], "end_turn"))

    queue: asyncio.Queue[Any] = asyncio.Queue()
    tailer = SessionTailer(tmp_path, queue, latest_finder=lambda _cwd: path)
    tailer._resolve_file()
    await tailer.poll_once()
    # 起動前からあった内容は再生しない（全文再生事故の防止）
    assert queue.empty()

    append_rows(path, assistant([{"type": "text", "text": "新"}], "end_turn"))
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("新")


async def test_does_not_follow_newer_file(tmp_path: Path) -> None:
    """追跡先を決めた後は、より新しい JSONL が現れても乗り換えない。

    同じ cwd で新セッションを起動する / cron が古いセッションを kill して
    mtime が動く、のどちらでもミラー元が黙って移らないことを保証する。
    """

    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.touch()
    second.touch()
    selected = first

    queue: asyncio.Queue[Any] = asyncio.Queue()
    tailer = SessionTailer(tmp_path, queue, latest_finder=lambda _cwd: selected)
    tailer._resolve_file()

    selected = second
    append_rows(second, assistant([{"type": "text", "text": "他"}], "end_turn"))
    await tailer.poll_once()
    assert queue.empty()

    append_rows(first, assistant([{"type": "text", "text": "本命"}], "end_turn"))
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("本命")


async def test_session_file_pins_target(tmp_path: Path) -> None:
    pinned = tmp_path / "pinned.jsonl"
    newest = tmp_path / "newest.jsonl"
    pinned.touch()

    queue: asyncio.Queue[Any] = asyncio.Queue()
    tailer = SessionTailer(
        tmp_path,
        queue,
        session_file=pinned,
        latest_finder=lambda _cwd: newest,
    )
    tailer._resolve_file()
    assert tailer._path == pinned

    append_rows(newest, assistant([{"type": "text", "text": "他"}], "end_turn"))
    append_rows(pinned, assistant([{"type": "text", "text": "本命"}], "end_turn"))
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("本命")
    assert queue.empty()


async def test_session_file_waits_until_created(tmp_path: Path) -> None:
    pinned = tmp_path / "later.jsonl"
    queue: asyncio.Queue[Any] = asyncio.Queue()
    tailer = SessionTailer(tmp_path, queue, session_file=pinned)

    tailer._resolve_file()
    assert tailer._path is None
    await tailer.poll_once()
    assert queue.empty()

    append_rows(pinned, assistant([{"type": "text", "text": "済"}], "end_turn"))
    await tailer.poll_once()  # ここで掴む（末尾からなので既存分は流さない）
    append_rows(pinned, assistant([{"type": "text", "text": "後"}], "end_turn"))
    await tailer.poll_once()
    assert await queue.get() == AssistantTurn("後")
