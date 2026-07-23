"""tmux と Claude Code セッションファイルに対する低レベル I/O。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import re


TmuxRunner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


class SessionIOError(RuntimeError):
    """tmux 操作に失敗したことを表す。"""


async def default_tmux_runner(argv: list[str]) -> tuple[int, str, str]:
    """サブプロセスで tmux を実行する既定 runner。"""

    process = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    return (
        process.returncode,
        stdout_bytes.decode(errors="replace"),
        stderr_bytes.decode(errors="replace"),
    )


def project_dir_for_cwd(cwd: str | Path) -> Path:
    """Claude Code が cwd に対して使用する project directory を返す。"""

    encoded = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    return Path.home() / ".claude" / "projects" / encoded


def latest_session_jsonl(cwd: str | Path) -> Path | None:
    """cwd に対応する最新 JSONL を返す。I/O エラーは未検出として扱う。"""

    try:
        candidates = list(project_dir_for_cwd(cwd).glob("*.jsonl"))
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)
    except OSError:
        return None


def _failure_reason(action: str, stderr: str) -> str:
    detail = stderr.strip()
    return f"{action}: {detail}" if detail else f"{action} failed"


async def send_prompt(
    runner: TmuxRunner,
    target: str,
    text: str,
) -> None:
    """リテラル文字列と Enter を別々の tmux 呼び出しで送る。"""

    argv = ["tmux", "send-keys", "-t", target, "-l", "--", text]
    rc, _stdout, stderr = await runner(argv)
    if rc != 0:
        raise SessionIOError(_failure_reason("tmux send-keys", stderr))

    rc, _stdout, stderr = await runner(
        ["tmux", "send-keys", "-t", target, "Enter"]
    )
    if rc != 0:
        raise SessionIOError(_failure_reason("tmux send Enter", stderr))


async def send_digit(
    runner: TmuxRunner,
    target: str,
    digit: str,
) -> None:
    """選択ダイアログへ数字キーだけを送る。Enter は送らない。"""

    rc, _stdout, stderr = await runner(
        ["tmux", "send-keys", "-t", target, digit]
    )
    if rc != 0:
        raise SessionIOError(_failure_reason("tmux send digit", stderr))


async def tmux_target_exists(runner: TmuxRunner, target: str) -> bool:
    """target pane が存在し、pane id を取得できるか返す。

    display-message は制御端末なし（デーモン実行）だと rc=0 のまま
    空文字を返すため、サーバー問い合わせで完結する list-panes を使う。
    """

    rc, stdout, _stderr = await runner(
        ["tmux", "list-panes", "-t", target, "-F", "#{pane_id}"]
    )
    return rc == 0 and bool(stdout.strip())


def _pane_index_of(target: str) -> str | None:
    """"session:window.pane" 形式から pane index を取り出す。無ければ None。"""

    _, _, tail = target.rpartition(".")
    return tail if tail.isdigit() else None


async def tmux_pane_cwd(runner: TmuxRunner, target: str) -> str:
    """target pane の現在の作業ディレクトリを返す。

    list-panes は window 内の全 pane を返すため、target に pane index が
    含まれていればその行を選ぶ（無ければ先頭行）。
    """

    rc, stdout, stderr = await runner(
        [
            "tmux",
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_index}\t#{pane_current_path}",
        ]
    )
    if rc != 0:
        raise SessionIOError(_failure_reason("tmux pane cwd", stderr))
    rows = [
        line.split("\t", 1)
        for line in stdout.splitlines()
        if "\t" in line
    ]
    if not rows:
        raise SessionIOError("tmux pane cwd is empty")
    wanted = _pane_index_of(target)
    for index, path in rows:
        if wanted is None or index == wanted:
            if not path:
                break
            return path
    raise SessionIOError("tmux pane cwd is empty")


async def capture_pane(runner: TmuxRunner, target: str) -> str:
    """target pane の表示内容を probe 用に取得する。"""

    rc, stdout, stderr = await runner(
        ["tmux", "capture-pane", "-t", target, "-p"]
    )
    if rc != 0:
        raise SessionIOError(_failure_reason("tmux capture-pane", stderr))
    return stdout

