"""watch2 の設定読み込みと検証。"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
import tomllib

from .session_io import project_dir_for_cwd


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """Discord チャンネルと tmux pane の対応設定。"""

    tmux_target: str
    cwd: Path
    session_file: Path | None = None


@dataclass(frozen=True, slots=True)
class AppConfig:
    """検証済みアプリケーション設定。"""

    projects: dict[int, ProjectConfig]


def _required_nonempty_string(project: dict[str, Any], key: str, index: int) -> str:
    value = project.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"projects[{index}].{key} must be a non-empty string")
    return value


def _session_file(
    project: dict[str, Any], index: int, cwd: Path
) -> Path | None:
    """追跡する JSONL を解決する。未設定なら None（起動時の最新に委ねる）。

    ファイル名や session id だけの指定も受け付け、cwd に対応する
    project directory 配下として解決する。
    """

    value = project.get("session_file")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"projects[{index}].session_file must be a non-empty string"
        )
    path = Path(value)
    if not path.is_absolute():
        name = path.name
        if not name.endswith(".jsonl"):
            name = f"{name}.jsonl"
        path = project_dir_for_cwd(cwd) / name
    return path


def load_config(path: str | Path = "claude-watch.toml") -> AppConfig:
    """TOML 設定を読み込み、必須項目を fail-fast で検証する。"""

    config_path = Path(path)
    with config_path.open("rb") as file:
        raw = tomllib.load(file)

    projects_raw = raw.get("projects")
    if not isinstance(projects_raw, list) or not projects_raw:
        raise ValueError("projects must be a non-empty array")

    projects: dict[int, ProjectConfig] = {}
    for index, project in enumerate(projects_raw):
        if not isinstance(project, dict):
            raise ValueError(f"projects[{index}] must be a table")

        channel_id = project.get("channel_id")
        if (
            not isinstance(channel_id, int)
            or isinstance(channel_id, bool)
        ):
            raise ValueError(f"projects[{index}].channel_id must be an integer")
        if channel_id in projects:
            raise ValueError(f"duplicate channel_id: {channel_id}")

        tmux_target = _required_nonempty_string(project, "tmux_target", index)
        cwd_text = _required_nonempty_string(project, "cwd", index)
        cwd = Path(cwd_text)
        if not cwd.is_absolute():
            raise ValueError(f"projects[{index}].cwd must be an absolute path")

        projects[channel_id] = ProjectConfig(
            tmux_target=tmux_target,
            cwd=cwd,
            session_file=_session_file(project, index, cwd),
        )

    return AppConfig(projects=projects)


def load_discord_token() -> str:
    """環境変数から Discord bot token を取得する。"""

    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    return token

