"""Discord client とチャンネル単位の状態機械。"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
import logging
import os
from typing import Any

import discord

from .config import ProjectConfig
from .probe import DialogDetected, DialogGone, DialogProbe, ProbeError
from .session_io import (
    SessionIOError,
    TmuxRunner,
    default_tmux_runner,
    send_digit,
    send_prompt,
    tmux_pane_cwd,
    tmux_target_exists,
)
from .tail import (
    ApprovalRequest,
    ApprovalResolved,
    AssistantTurn,
    SessionTailer,
    TailEvent,
    UserPrompt,
)


LOGGER = logging.getLogger(__name__)
MESSAGE_CHUNK_SIZE = 1900


class StateKind(Enum):
    NORMAL = "normal"
    AWAITING = "awaiting"


@dataclass(frozen=True, slots=True)
class ChannelState:
    kind: StateKind = StateKind.NORMAL
    source: str | None = None


NORMAL_STATE = ChannelState()
BotEvent = TailEvent | DialogDetected | DialogGone | ProbeError


async def send_chunked(destination: Any, text: str) -> None:
    """Discord の投稿を 1900 文字固定長で分割する。"""

    if not text:
        return
    for start in range(0, len(text), MESSAGE_CHUNK_SIZE):
        await destination.send(text[start : start + MESSAGE_CHUNK_SIZE])


def _warning(reason: str) -> str:
    reason = " ".join(str(reason).splitlines()).strip()
    return f"⚠️ {reason or '不明なエラー'}"


class WatchClient(discord.Client):
    """tmux セッションごとの tail/probe と Discord を接続する client。"""

    def __init__(
        self,
        projects: dict[int, ProjectConfig],
        *,
        runner: TmuxRunner = default_tmux_runner,
        **kwargs: Any,
    ) -> None:
        intents = kwargs.pop("intents", None)
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
        super().__init__(intents=intents, **kwargs)
        self.projects = projects
        self.runner = runner
        self.states = {channel_id: NORMAL_STATE for channel_id in projects}
        self.recent_sent = {
            channel_id: deque(maxlen=5) for channel_id in projects
        }
        self._background_tasks: list[asyncio.Task[Any]] = []
        self._tailers: list[SessionTailer] = []
        self._probes: list[DialogProbe] = []
        self._workers_started = False
        self._input_locks: dict[str, asyncio.Lock] = {}

    async def on_ready(self) -> None:
        if self._workers_started:
            return
        self._workers_started = True
        for channel_id, project in self.projects.items():
            queue: asyncio.Queue[BotEvent] = asyncio.Queue()
            tailer = SessionTailer(
                project.cwd, queue, session_file=project.session_file
            )
            probe = DialogProbe(
                self.runner, project.tmux_target, queue
            )
            self._tailers.append(tailer)
            self._probes.append(probe)
            self._background_tasks.extend(
                [
                    asyncio.create_task(
                        tailer.run(), name=f"tail-{channel_id}"
                    ),
                    asyncio.create_task(
                        probe.run(), name=f"probe-{channel_id}"
                    ),
                    asyncio.create_task(
                        self._dispatch_events(channel_id, queue),
                        name=f"events-{channel_id}",
                    ),
                ]
            )
        LOGGER.info("watch2 ready as %s", self.user)

    async def close(self) -> None:
        for tailer in self._tailers:
            tailer.stop()
        for probe in self._probes:
            probe.stop()
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(
                *self._background_tasks, return_exceptions=True
            )
        await super().close()

    async def _dispatch_events(
        self, channel_id: int, queue: asyncio.Queue[BotEvent]
    ) -> None:
        while True:
            event = await queue.get()
            try:
                await self.handle_event(channel_id, event)
            except Exception:
                LOGGER.exception("event handling failed for %s", channel_id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        channel_id = message.channel.id
        if channel_id not in self.projects:
            LOGGER.debug("ignored unmapped channel %s", channel_id)
            return
        text = message.content.strip()
        if not text:
            return
        target = self.projects[channel_id].tmux_target
        lock = self._input_locks.setdefault(target, asyncio.Lock())
        async with lock:
            await self._deliver_message(message, channel_id, text)

    async def _deliver_message(
        self, message: discord.Message, channel_id: int, text: str
    ) -> None:
        # Discord dispatches handlers concurrently. Keep validation and the
        # literal-text/Enter pair together for one configured pane target.
        if text == "!reset":
            self.states[channel_id] = NORMAL_STATE
            await message.reply("✅ 承認待ち状態を解除しました")
            return

        state = self.states[channel_id]
        if state.kind is StateKind.AWAITING:
            if len(text) == 1 and text in "123456789":
                await self._send_approval_digit(message, text)
            else:
                await message.reply(
                    "⚠️ 承認の回答待ちです（番号で返信）。解除は !reset"
                )
            return

        project = self.projects[channel_id]
        try:
            await self._validate_session(project)
            await send_prompt(
                self.runner, project.tmux_target, message.content
            )
        except (SessionIOError, OSError) as error:
            await message.reply(_warning(str(error)))
            return
        self.recent_sent[channel_id].append(message.content)
        await message.add_reaction("✅")

    async def _send_approval_digit(
        self, message: discord.Message, digit: str
    ) -> None:
        project = self.projects[message.channel.id]
        try:
            await self._validate_session(project)
            await send_digit(self.runner, project.tmux_target, digit)
        except (SessionIOError, OSError) as error:
            await message.reply(_warning(str(error)))

    async def _validate_session(self, project: ProjectConfig) -> None:
        if not await tmux_target_exists(self.runner, project.tmux_target):
            raise SessionIOError(
                f"tmux pane が見つかりません: {project.tmux_target}"
            )
        pane_cwd = await tmux_pane_cwd(self.runner, project.tmux_target)
        if os.path.realpath(pane_cwd) != os.path.realpath(project.cwd):
            raise SessionIOError(
                f"pane cwd が設定と一致しません: {pane_cwd}"
            )

    async def handle_event(self, channel_id: int, event: BotEvent) -> None:
        """tail/probe イベントを状態遷移させ Discord へ投稿する。"""

        channel = self.get_channel(channel_id)
        if channel is None:
            LOGGER.error("mapped channel is not available: %s", channel_id)
            return

        if isinstance(event, AssistantTurn):
            await send_chunked(channel, event.text)
        elif isinstance(event, ApprovalRequest):
            await send_chunked(channel, self._format_approval(event))
            self.states[channel_id] = ChannelState(
                StateKind.AWAITING, f"jsonl:{event.tool_use_id}"
            )
        elif isinstance(event, ApprovalResolved):
            state = self.states[channel_id]
            if state.source == f"jsonl:{event.tool_use_id}":
                self.states[channel_id] = NORMAL_STATE
            await send_chunked(
                channel, f"✅ 回答: {self._summarize_answers(event)}"
            )
        elif isinstance(event, DialogDetected):
            state = self.states[channel_id]
            if state.kind is StateKind.AWAITING and (
                state.source or ""
            ).startswith("jsonl:"):
                return
            if state.kind is StateKind.NORMAL:
                await send_chunked(
                    channel,
                    f"```\n{event.screen_excerpt}\n```\n番号で返信できます",
                )
                self.states[channel_id] = ChannelState(
                    StateKind.AWAITING, "pane"
                )
        elif isinstance(event, DialogGone):
            if self.states[channel_id].source == "pane":
                await send_chunked(channel, "✅ ダイアログ解決")
                self.states[channel_id] = NORMAL_STATE
        elif isinstance(event, ProbeError):
            await send_chunked(channel, _warning(event.reason))
        elif isinstance(event, UserPrompt):
            recent = self.recent_sent[channel_id]
            try:
                recent.remove(event.text)
            except ValueError:
                await send_chunked(channel, f"🖥️ {event.text}")

    @staticmethod
    def _format_approval(event: ApprovalRequest) -> str:
        lines: list[str] = []
        emoji_numbers = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")
        for question in event.questions:
            if question.header:
                lines.append(question.header)
            lines.append(question.question)
            if question.multi_select:
                lines.append(
                    "⚠️ 複数選択の質問です。端末での回答を推奨"
                )
            for index, option in enumerate(question.options, start=1):
                marker = (
                    emoji_numbers[index - 1]
                    if index <= 9
                    else f"{index}."
                )
                suffix = (
                    f" — {option.description}"
                    if option.description
                    else ""
                )
                lines.append(f"{marker} {option.label}{suffix}")
        lines.append(
            "（端末にはこの下に自由記述などの追加選択肢が付くことがあります。"
            "数字はそのまま TUI に送られます）"
        )
        return "\n".join(lines)

    @staticmethod
    def _summarize_answers(event: ApprovalResolved) -> str:
        if not event.answers:
            return "回答済み"
        return " / ".join(
            f"{question} → {answer}"
            for question, answer in event.answers.items()
        )

