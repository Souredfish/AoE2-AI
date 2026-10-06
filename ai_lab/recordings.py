"""Locate AoE2 DE recordings across the Windows savegame layouts in use."""

import os
from pathlib import Path


def recording_directories(cfg, environ=None):
    """Return configured and standard DE recording directories in priority order."""
    environ = os.environ if environ is None else environ
    result = []
    configured = cfg.get("game", {}).get("recordings_dir")
    if configured:
        result.append(Path(configured).expanduser())

    profile = environ.get("USERPROFILE")
    if profile:
        home = Path(profile)
        roots = (
            home / "Games" / "Age of Empires 2 DE",
            home / "Documents" / "My Games" / "Age of Empires 2 DE",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for savegame in sorted(root.glob("*/savegame")):
                result.extend((savegame / "multi", savegame))

    unique = []
    seen = set()
    for directory in result:
        key = os.path.normcase(os.path.abspath(str(directory)))
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def list_recordings(cfg, environ=None):
    """Return discovered recordings, keyed by normalized absolute path."""
    recordings = {}
    for directory in recording_directories(cfg, environ):
        if directory.is_dir():
            for path in directory.glob("*.aoe2record"):
                recordings[os.path.normcase(os.path.abspath(str(path)))] = path
    return recordings


def latest_recording(cfg, environ=None):
    recordings = list_recordings(cfg, environ).values()
    return max(recordings, key=lambda path: path.stat().st_mtime) if recordings else None
