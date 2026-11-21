"""Read the systemd journal through `journalctl -o json` (Linux with systemd only).

The JSON adapter in formats.py already understands journald's fields (PRIORITY, MESSAGE,
SYSLOG_IDENTIFIER, __REALTIME_TIMESTAMP), so this module only has to run the command safely.
You may need `sudo` or membership of the `systemd-journal` group to see everything.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Callable, Iterator, Optional

from ..models import RawLine
from . import SourceError

_MISSING = "journalctl was not found. Reading the systemd journal needs Linux with systemd."


def build_command(unit: Optional[str] = None, since: Optional[datetime] = None, follow: bool = False) -> list[str]:
    cmd = ["journalctl", "--no-pager", "-o", "json"]
    if follow:
        cmd += ["-f", "-n", "0"]  # only new entries
    elif since is not None:
        cmd += ["--since", since.strftime("%Y-%m-%d %H:%M:%S")]
    if unit:
        cmd += ["-u", unit]
    return cmd


def _start(cmd: list[str], stderr) -> "subprocess.Popen[str]":
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise SourceError(_MISSING) from None
    except PermissionError:
        raise SourceError("Permission denied running journalctl.") from None


def read_journal(unit: Optional[str], since_seconds: float) -> Iterator[RawLine]:
    """Yield the journal entries of the last `since_seconds` (optionally for one unit)."""
    since = datetime.now() - timedelta(seconds=since_seconds)
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as err:
        proc = _start(build_command(unit, since), err)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                yield RawLine(line)
        proc.wait()
        if proc.returncode not in (0, None):
            err.seek(0)
            detail = (err.read().strip().splitlines() or ["no details"])[0][:200]
            raise SourceError(f"journalctl failed: {detail}")


def follow_journal(
    unit: Optional[str],
    put: Callable[[RawLine], None],
    stop: threading.Event,
    *,
    retry: float = 5.0,
    report: Optional[Callable[[str], None]] = None,
) -> None:
    """Follow new journal entries, restarting journalctl if it exits."""
    cmd = build_command(unit, follow=True)
    while not stop.is_set():
        try:
            proc = _start(cmd, subprocess.DEVNULL)
        except SourceError as exc:
            if report:
                report(str(exc))
            return  # not recoverable: there is no journalctl to retry
        done = threading.Event()

        def killer(p=proc, d=done) -> None:
            while not (stop.is_set() or d.is_set()):
                stop.wait(0.2)
            if stop.is_set():
                p.terminate()

        threading.Thread(target=killer, daemon=True).start()
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if line:
                put(RawLine(line))
        proc.wait()
        done.set()
        if stop.is_set():
            return
        if report:
            report("journalctl stopped unexpectedly; retrying")
        stop.wait(retry)
