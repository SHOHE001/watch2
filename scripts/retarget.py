#!/usr/bin/env python3
"""watch2 の接続先セッションを付け替えるヘルパー。

使い方（リポジトリ root で実行）:

    .venv/bin/python scripts/retarget.py <tmux_target> [--channel CHANNEL_ID]

例:

    .venv/bin/python scripts/retarget.py main:0.0

<tmux_target> の pane から作業ディレクトリを自動検出し、
claude-watch.toml の該当エントリを書き換えて systemd の watch2 を再起動する。
--channel は対応表に複数エントリがあるときだけ必須（1件ならそれを書き換える）。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import tomllib

CONFIG_PATH = Path("claude-watch.toml")


def pane_cwd(target: str) -> str:
    """target pane の作業ディレクトリを list-panes で取得する。

    display-message は制御端末なしで空文字を返すため使わない
    （watch2.session_io と同じ理由）。
    """

    result = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-t",
            target,
            "-F",
            "#{pane_index}\t#{pane_current_path}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"エラー: tmux pane が見つかりません: {target}\n{result.stderr.strip()}")
    rows = [line.split("\t", 1) for line in result.stdout.splitlines() if "\t" in line]
    _, _, tail = target.rpartition(".")
    wanted = tail if tail.isdigit() else None
    for index, path in rows:
        if wanted is None or index == wanted:
            return path
    sys.exit(f"エラー: pane index {wanted} が {target} に見つかりません")


def emit_toml(projects: list[dict]) -> str:
    lines = [
        "# claude-watch.toml — Discord channel ↔ 対話セッション紐付け",
        "# machine-local。gitignore 対象。scripts/retarget.py で書き換え可能。",
        "",
    ]
    for project in projects:
        lines += [
            "[[projects]]",
            f"channel_id = {project['channel_id']}",
            f'tmux_target = "{project["tmux_target"]}"',
            f'cwd = "{project["cwd"]}"',
            "",
        ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="watch2 の接続先を付け替える")
    parser.add_argument("tmux_target", help='付け替え先の pane（例 "main:0.0"）')
    parser.add_argument(
        "--channel",
        type=int,
        default=None,
        help="書き換える channel_id（対応表が1件のときは省略可）",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="systemd の watch2 を再起動しない",
    )
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        sys.exit(f"エラー: {CONFIG_PATH} がありません（リポジトリ root で実行すること）")
    with CONFIG_PATH.open("rb") as file:
        projects = tomllib.load(file).get("projects", [])
    if not projects:
        sys.exit("エラー: claude-watch.toml に [[projects]] がありません")

    if args.channel is None:
        if len(projects) > 1:
            ids = ", ".join(str(p["channel_id"]) for p in projects)
            sys.exit(f"エラー: エントリが複数あります。--channel で指定してください: {ids}")
        entry = projects[0]
    else:
        matches = [p for p in projects if p["channel_id"] == args.channel]
        if not matches:
            sys.exit(f"エラー: channel_id {args.channel} は対応表にありません")
        entry = matches[0]

    cwd = pane_cwd(args.tmux_target)
    before = f'{entry["tmux_target"]} ({entry["cwd"]})'
    entry["tmux_target"] = args.tmux_target
    entry["cwd"] = cwd
    CONFIG_PATH.write_text(emit_toml(projects), encoding="utf-8")
    print(f"付け替え: {before}")
    print(f"      → {args.tmux_target} ({cwd})  [channel {entry['channel_id']}]")

    if args.no_restart:
        print("--no-restart 指定のため再起動していません（反映には再起動が必要）")
        return
    restart = subprocess.run(
        ["sudo", "-n", "systemctl", "restart", "watch2"],
        capture_output=True,
        text=True,
    )
    if restart.returncode != 0:
        print("watch2 の再起動に失敗しました。手動で実行してください:")
        print("  sudo systemctl restart watch2")
        print(restart.stderr.strip())
        return
    print("watch2 を再起動しました。journalctl -u watch2 -n 3 で確認できます")


if __name__ == "__main__":
    main()
