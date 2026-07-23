"""Claude Code セッション JSONL の常時 tail とイベント抽出。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any

from .session_io import latest_session_jsonl


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    text: str


@dataclass(frozen=True, slots=True)
class ApprovalOption:
    label: str
    description: str


@dataclass(frozen=True, slots=True)
class ApprovalQuestion:
    question: str
    header: str
    multi_select: bool
    options: tuple[ApprovalOption, ...]


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    tool_use_id: str
    questions: tuple[ApprovalQuestion, ...]


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    tool_use_id: str
    answers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class UserPrompt:
    text: str


TailEvent = AssistantTurn | ApprovalRequest | ApprovalResolved | UserPrompt
LatestSessionFinder = Callable[[str | Path], Path | None]


class SessionTailer:
    """1 プロジェクトの最新 JSONL を追跡してイベントを発行する。"""

    def __init__(
        self,
        cwd: str | Path,
        queue: asyncio.Queue[TailEvent],
        *,
        poll_interval: float = 1.0,
        refresh_interval: float = 5.0,
        latest_finder: LatestSessionFinder = latest_session_jsonl,
    ) -> None:
        self.cwd = Path(cwd)
        self.queue = queue
        self.poll_interval = poll_interval
        self.refresh_interval = refresh_interval
        self.latest_finder = latest_finder
        self._path: Path | None = None
        self._offset = 0
        self._carry = b""
        self._assistant_text: list[str] = []
        self._pending_ids: set[str] = set()
        self._batch: list[TailEvent] = []
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        """tail loop の停止を要求する。"""

        self._stopped.set()

    async def run(self) -> None:
        """停止要求まで JSONL をポーリングする。"""

        self._select_initial_file()
        next_refresh = time.monotonic() + self.refresh_interval
        while not self._stopped.is_set():
            now = time.monotonic()
            if now >= next_refresh:
                self._refresh_file()
                next_refresh = now + self.refresh_interval
            await self.poll_once()
            try:
                await asyncio.wait_for(
                    self._stopped.wait(), timeout=self.poll_interval
                )
            except TimeoutError:
                pass

    def _select_initial_file(self) -> None:
        path = self.latest_finder(self.cwd)
        self._path = path
        if path is None:
            return
        try:
            self._offset = path.stat().st_size
        except OSError:
            self._path = None
            self._offset = 0

    def _refresh_file(self) -> None:
        path = self.latest_finder(self.cwd)
        if path == self._path:
            return
        self._path = path
        self._offset = 0
        self._carry = b""
        self._assistant_text.clear()
        self._pending_ids.clear()

    async def poll_once(self) -> None:
        """現在のファイルから追加された完全行を一度だけ処理する。"""

        if self._path is None:
            self._refresh_file()
            return
        try:
            with self._path.open("rb") as file:
                file.seek(self._offset)
                chunk = file.read()
                self._offset = file.tell()
        except OSError:
            return
        if not chunk:
            return

        pieces = (self._carry + chunk).split(b"\n")
        self._carry = pieces.pop()
        for raw_line in pieces:
            if not raw_line:
                continue
            try:
                row = json.loads(raw_line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            await self._process_row(row)
        await self._flush_batch()

    async def _flush_batch(self) -> None:
        """同一バッチ内で解決済みの承認要求を落としてから配信する。

        AskUserQuestion の tool_use 行はダイアログ表示時ではなく回答後に
        まとめて書かれることがあり、その場合 request → resolved が同じ
        ポーリングで届く。既に終わった要求を投稿しても混乱するだけなので
        request 側だけ捨てる（resolved は回答内容の通知として残す）。
        """

        batch = self._batch
        self._batch = []
        resolved_ids = {
            event.tool_use_id
            for event in batch
            if isinstance(event, ApprovalResolved)
        }
        for event in batch:
            if (
                isinstance(event, ApprovalRequest)
                and event.tool_use_id in resolved_ids
            ):
                continue
            await self.queue.put(event)

    async def _flush_assistant(self) -> None:
        if not self._assistant_text:
            return
        text = "\n\n".join(self._assistant_text)
        self._assistant_text.clear()
        if text:
            self._batch.append(AssistantTurn(text))

    async def _process_row(self, row: Any) -> None:
        if not isinstance(row, dict):
            return
        if row.get("type") not in {"assistant", "user"}:
            return
        if row.get("isSidechain") is True:
            return
        if row["type"] == "assistant":
            await self._process_assistant(row)
        else:
            await self._process_user(row)

    async def _process_assistant(self, row: dict[str, Any]) -> None:
        message = row.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text" and isinstance(block.get("text"), str):
                    self._assistant_text.append(block["text"])
                elif (
                    block_type == "tool_use"
                    and block.get("name") == "AskUserQuestion"
                ):
                    await self._flush_assistant()
                    event = self._approval_request(block)
                    if event is not None:
                        self._pending_ids.add(event.tool_use_id)
                        self._batch.append(event)

        if message.get("stop_reason") == "end_turn":
            await self._flush_assistant()

    def _approval_request(
        self, block: dict[str, Any]
    ) -> ApprovalRequest | None:
        tool_use_id = block.get("id")
        input_value = block.get("input")
        if not isinstance(tool_use_id, str) or not isinstance(input_value, dict):
            return None
        raw_questions = input_value.get("questions")
        if not isinstance(raw_questions, list):
            return None

        questions: list[ApprovalQuestion] = []
        for raw_question in raw_questions:
            if not isinstance(raw_question, dict):
                continue
            options: list[ApprovalOption] = []
            raw_options = raw_question.get("options")
            if isinstance(raw_options, list):
                for option in raw_options:
                    if not isinstance(option, dict):
                        continue
                    label = option.get("label")
                    description = option.get("description")
                    if isinstance(label, str):
                        options.append(
                            ApprovalOption(
                                label=label,
                                description=(
                                    description
                                    if isinstance(description, str)
                                    else ""
                                ),
                            )
                        )
            question = raw_question.get("question")
            header = raw_question.get("header")
            questions.append(
                ApprovalQuestion(
                    question=question if isinstance(question, str) else "",
                    header=header if isinstance(header, str) else "",
                    multi_select=raw_question.get("multiSelect") is True,
                    options=tuple(options),
                )
            )
        return ApprovalRequest(tool_use_id, tuple(questions))

    async def _process_user(self, row: dict[str, Any]) -> None:
        message = row.get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")

        if isinstance(content, list):
            tool_results = [
                block
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_result"
            ]
            if tool_results:
                for block in tool_results:
                    tool_use_id = block.get("tool_use_id")
                    if (
                        isinstance(tool_use_id, str)
                        and tool_use_id in self._pending_ids
                    ):
                        answers = self._answers_from(row, block)
                        self._pending_ids.remove(tool_use_id)
                        self._batch.append(
                            ApprovalResolved(tool_use_id, answers)
                        )
                return

        prompt = self._plain_user_text(content)
        if prompt:
            self._batch.append(UserPrompt(prompt))

    @staticmethod
    def _answers_from(
        row: dict[str, Any], block: dict[str, Any]
    ) -> Mapping[str, str]:
        for owner in (row, block):
            result = owner.get("toolUseResult")
            if not isinstance(result, dict):
                continue
            answers = result.get("answers")
            if isinstance(answers, dict):
                return {
                    str(question): str(answer)
                    for question, answer in answers.items()
                }
        return {}

    @staticmethod
    def _plain_user_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        return "".join(texts)

