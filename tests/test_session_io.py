from pathlib import Path

import pytest

from watch2.session_io import (
    tmux_pane_cwd,
    SessionIOError,
    project_dir_for_cwd,
    send_digit,
    send_prompt,
    tmux_target_exists,
)


def test_project_dir_sanitizes_japanese(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = project_dir_for_cwd("/home/shohei/プロジェクト/Apple Watch２")
    assert result.parent == tmp_path / ".claude" / "projects"
    assert result.name == "-home-shohei--------Apple-Watch-"


async def test_send_prompt_uses_two_calls() -> None:
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return 0, "", ""

    await send_prompt(runner, "work:0.0", "日本語 'quoted'")
    assert calls == [
        [
            "tmux", "send-keys", "-t", "work:0.0", "-l", "--",
            "日本語 'quoted'",
        ],
        ["tmux", "send-keys", "-t", "work:0.0", "Enter"],
    ]


async def test_send_digit_does_not_send_enter() -> None:
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return 0, "", ""

    await send_digit(runner, "work:0.0", "3")
    assert calls == [["tmux", "send-keys", "-t", "work:0.0", "3"]]


async def test_send_failure_raises() -> None:
    async def runner(_argv: list[str]) -> tuple[int, str, str]:
        return 1, "", "pane missing"

    with pytest.raises(SessionIOError, match="pane missing"):
        await send_prompt(runner, "missing", "hello")


@pytest.mark.parametrize(
    ("rc", "stdout", "expected"),
    [(0, "%1\n", True), (0, "", False), (1, "%1\n", False)],
)
async def test_target_exists(
    rc: int, stdout: str, expected: bool
) -> None:
    async def runner(_argv: list[str]) -> tuple[int, str, str]:
        return rc, stdout, ""

    assert await tmux_target_exists(runner, "work") is expected


async def test_target_exists_uses_list_panes() -> None:
    # display-message は制御端末なしで rc=0・空文字を返すため使わない
    calls: list[list[str]] = []

    async def runner(argv: list[str]) -> tuple[int, str, str]:
        calls.append(argv)
        return 0, "%1\n", ""

    assert await tmux_target_exists(runner, "work:0.0") is True
    assert calls == [
        ["tmux", "list-panes", "-t", "work:0.0", "-F", "#{pane_id}"]
    ]


async def test_pane_cwd_selects_target_pane_index() -> None:
    async def runner(_argv: list[str]) -> tuple[int, str, str]:
        return 0, "0\t/tmp/a\n1\t/tmp/b\n", ""

    assert await tmux_pane_cwd(runner, "work:0.1") == "/tmp/b"
    assert await tmux_pane_cwd(runner, "work") == "/tmp/a"


async def test_pane_cwd_empty_raises() -> None:
    async def runner(_argv: list[str]) -> tuple[int, str, str]:
        return 0, "", ""

    with pytest.raises(SessionIOError):
        await tmux_pane_cwd(runner, "work:0.0")
