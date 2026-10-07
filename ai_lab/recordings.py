"""Locate AoE2 DE recordings across the Windows savegame layouts in use."""

import os
import stat
from pathlib import Path


def recording_directories(cfg, environ=None, *, strict=False):
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
            if strict:
                try:
                    if not _directory_exists_or_missing(root):
                        continue
                    with os.scandir(root) as entries:
                        children = list(entries)
                except OSError as exc:
                    raise RecordingDirectoryError(root, "enumerate", exc) from exc
                for child in children:
                    try:
                        if not child.is_dir(follow_symlinks=True):
                            continue
                    except OSError as exc:
                        raise RecordingDirectoryError(child.path, "inspect", exc) from exc
                    savegame = Path(child.path) / "savegame"
                    try:
                        if not _directory_exists_or_missing(savegame):
                            continue
                    except OSError as exc:
                        raise RecordingDirectoryError(savegame, "inspect", exc) from exc
                    result.extend((savegame / "multi", savegame))
                continue

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


class RecordingDirectoryError(OSError):
    """An expected replay directory exists but cannot be inspected safely."""

    def __init__(self, path, operation, cause):
        super().__init__("%s failed for %s: %s" % (operation, path, cause))
        self.path = str(path)
        self.operation = operation
        self.cause = cause


def _directory_exists_or_missing(path):
    """Return False only for absence; propagate access and wrong-type errors."""
    try:
        info = Path(path).stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise NotADirectoryError("expected directory: %s" % path)
    return True


def list_recordings(cfg, environ=None, *, strict=False, diagnostic=None,
                    diagnostic_context=None):
    """Return discovered recordings, keyed by normalized absolute path."""
    recordings = {}
    try:
        directories = recording_directories(cfg, environ, strict=strict)
    except RecordingDirectoryError as exc:
        if diagnostic is not None:
            _emit_directory_error(diagnostic, exc, diagnostic_context)
        if strict:
            raise
        directories = []

    for directory in directories:
        if not strict:
            if directory.is_dir():
                for path in directory.glob("*.aoe2record"):
                    recordings[os.path.normcase(os.path.abspath(str(path)))] = path
            continue

        try:
            if not _directory_exists_or_missing(directory):
                continue
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.endswith(".aoe2record"):
                        path = Path(entry.path)
                        recordings[os.path.normcase(os.path.abspath(str(path)))] = path
        except OSError as cause:
            exc = RecordingDirectoryError(directory, "enumerate", cause)
            if diagnostic is not None:
                _emit_directory_error(diagnostic, exc, diagnostic_context)
            raise exc from cause
    return recordings


def _emit_directory_error(diagnostic, error, context):
    payload = {
        "event": "recording_directory_error",
        "path": error.path,
        "operation": error.operation,
        "error_type": type(error.cause).__name__,
        "error": str(error.cause),
        **(context or {}),
    }
    try:
        diagnostic(payload)
    except Exception:
        pass


def latest_recording(cfg, environ=None):
    recordings = list_recordings(cfg, environ).values()
    # File timestamp resolution can tie on Windows; use the normalized path as
    # a stable secondary key so discovery does not depend on directory order.
    return max(
        recordings,
        key=lambda path: (
            path.stat().st_mtime_ns,
            os.path.normcase(os.path.abspath(str(path))),
        ),
    ) if recordings else None
