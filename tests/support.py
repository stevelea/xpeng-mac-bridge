"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

from xpengmac import config as config_module


def test_config(tmpdir: Path, **overrides):
    """A Config whose state files live inside ``tmpdir``.

    This exists because of a real bug. `Bridge` writes its state next to the
    config file, and a test config built with no path falls back to
    ``~/.config/xpeng-mac-bridge/config.json`` — the *real* one. The suite
    therefore overwrote the running daemon's ``availability_topics.json`` with
    the fixture VIN, and the daemon then spent its cycles clearing
    ``xpeng/l1nnsgha0sb000000/availability``, a topic belonging to a car that
    does not exist, while the actual car's entities sat there with no
    availability message and Home Assistant showed every one of them as
    unavailable.

    Pointing the config path at a temporary directory fixes it at the root:
    `default_state_file()` derives from `config.path`, so every derived file
    lands in the temporary directory whether or not a test remembers to pass
    ``state_file=``.
    """
    merged = {"mqtt.host": "127.0.0.1", **overrides}
    return config_module.load(tmpdir / "config.json", overrides=merged)
