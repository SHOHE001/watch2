from pathlib import Path

import pytest

from watch2.config import load_config, load_discord_token


def test_load_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[[projects]]\nchannel_id=123\ntmux_target='work:0.0'\ncwd='/srv/work'\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.projects[123].tmux_target == "work:0.0"
    assert config.projects[123].cwd == Path("/srv/work")


@pytest.mark.parametrize(
    "toml",
    [
        "",
        "projects = {}",
        "projects = []",
        "[[projects]]\ntmux_target='x'\ncwd='/tmp'",
        "[[projects]]\nchannel_id=true\ntmux_target='x'\ncwd='/tmp'",
        "[[projects]]\nchannel_id='1'\ntmux_target='x'\ncwd='/tmp'",
        "[[projects]]\nchannel_id=1\ncwd='/tmp'",
        "[[projects]]\nchannel_id=1\ntmux_target='x'",
        "[[projects]]\nchannel_id=1\ntmux_target='x'\ncwd='relative'",
        (
            "[[projects]]\nchannel_id=1\ntmux_target='x'\ncwd='/tmp/a'\n"
            "[[projects]]\nchannel_id=1\ntmux_target='y'\ncwd='/tmp/b'"
        ),
    ],
)
def test_invalid_config_fails_fast(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
    assert load_discord_token() == "secret"
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    with pytest.raises(ValueError):
        load_discord_token()
