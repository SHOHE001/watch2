"""capture-pane を使った選択ダイアログ検知。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import re

from .session_io import SessionIOError, TmuxRunner, capture_pane


@dataclass(frozen=True, slots=True)
class DialogDetected:
    screen_excerpt: str


@dataclass(frozen=True, slots=True)
class DialogGone:
    pass


@dataclass(frozen=True, slots=True)
class ProbeError:
    reason: str


ProbeEvent = DialogDetected | DialogGone | ProbeError
_DIALOG_MARKER = re.compile(r"^\s*❯ \d+\. ", re.MULTILINE)
_EXCERPT_LINES_BEFORE_MARKER = 8
_EXCERPT_MAX_LINES = 25


def _dialog_excerpt(screen: str) -> str:
    """カーソル行を起点に、質問文を含む前後だけを抜き出す。

    画面全体（スクロールバック混じり）を送ると Discord 側で読みにくい
    ため、ダイアログのカーソル行から数行さかのぼった範囲に絞る。
    """

    nonempty_lines = [line for line in screen.splitlines() if line.strip()]
    start = 0
    for index, line in enumerate(nonempty_lines):
        if _DIALOG_MARKER.match(line):
            start = max(0, index - _EXCERPT_LINES_BEFORE_MARKER)
            break
    return "\n".join(nonempty_lines[start:][:_EXCERPT_MAX_LINES])


class DialogProbe:
    """tmux pane を定期取得し、選択ダイアログの出現と消滅を通知する。"""

    def __init__(
        self,
        runner: TmuxRunner,
        target: str,
        queue: asyncio.Queue[ProbeEvent],
        *,
        poll_interval: float = 3.0,
    ) -> None:
        self.runner = runner
        self.target = target
        self.queue = queue
        self.poll_interval = poll_interval
        self._detected = False
        self._error_reported = False
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    async def run(self) -> None:
        while not self._stopped.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.poll_interval
                )
            except TimeoutError:
                pass

    async def poll_once(self) -> None:
        """capture-pane を一度実行して状態変化を発行する。"""

        try:
            screen = await capture_pane(self.runner, self.target)
        except SessionIOError as error:
            if not self._error_reported:
                await self.queue.put(ProbeError(str(error)))
                self._error_reported = True
            return

        self._error_reported = False
        has_dialog = _DIALOG_MARKER.search(screen) is not None
        if has_dialog and not self._detected:
            await self.queue.put(DialogDetected(_dialog_excerpt(screen)))
        elif not has_dialog and self._detected:
            await self.queue.put(DialogGone())
        self._detected = has_dialog

