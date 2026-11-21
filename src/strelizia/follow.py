"""Follow live log sources and feed windows of records to the engine.

Readers run in background threads and only push lines onto a queue. The main loop
(Watcher.run) owns everything else: it assembles multi-line records, cuts the stream
into fixed windows, and hands each window to the Engine. Only the main thread ever
touches the database.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from .assembler import Assembler
from .formats import ALL_FORMATS
from .models import LogRecord, RawLine
from .sources import SourceError
from .sources.docker import ContainerInfo, _get_container, inspect_container, split_docker_prefix
from .watch import Engine, WindowResult

Put = Callable[[RawLine], None]
Reader = Callable[[Put, threading.Event], None]


@dataclass
class Source:
    name: str
    reader: Reader
    container: Optional[str] = None  # set for Docker sources, so restarts can be detected


# ---------------------------------------------------------------------- readers


def follow_file(path: str, put: Put, stop: threading.Event, *, from_start: bool = False, poll: float = 0.2) -> None:
    """Like `tail -F`: follows a file, survives rotation and truncation."""
    fh = None
    inode = None
    buf = ""
    opened_before = False
    while not stop.is_set():
        if fh is None:
            try:
                fh = open(path, encoding="utf-8", errors="replace", newline="")
            except OSError:
                opened_before = True  # a file that appears later is read from its start
                stop.wait(poll * 5)
                continue
            inode = os.fstat(fh.fileno()).st_ino
            if not opened_before and not from_start:
                fh.seek(0, os.SEEK_END)
            opened_before = True
        line = fh.readline()
        if line:
            buf += line
            if buf.endswith("\n"):
                put(RawLine(buf.rstrip("\r\n")))
                buf = ""
            continue
        try:
            st = os.stat(path)
        except OSError:  # file disappeared (rotation in progress)
            fh.close()
            fh, buf = None, ""
            continue
        rotated = bool(inode) and bool(st.st_ino) and st.st_ino != inode
        if rotated or st.st_size < fh.tell():
            fh.close()
            fh, buf = None, ""
            continue
        stop.wait(poll)
    if fh is not None:
        fh.close()


def follow_docker(name: str, put: Put, stop: threading.Event, *, retry: float = 3.0,
                  report: Optional[Callable[[str], None]] = None) -> None:
    """Follow a container's logs, reconnecting after restarts or a lost connection."""
    since = int(time.time())  # whole seconds: older docker-py versions reject floats
    skip_until: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    while not stop.is_set():
        buf = b""
        try:
            container = _get_container(name)
            stream = container.logs(stream=True, follow=True, since=since, timestamps=True, stdout=True, stderr=True)
            for chunk in stream:
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    raw = split_docker_prefix(raw_line.decode("utf-8", errors="replace").rstrip("\r"))
                    if raw.ts is not None:
                        if skip_until is not None and raw.ts <= skip_until:
                            continue  # already delivered before the reconnect
                        skip_until = None
                        last_ts = raw.ts
                    put(raw)
                if stop.is_set():
                    return
        except SourceError as exc:
            if report:
                report(str(exc))
        except Exception as exc:  # network blips, daemon restarts...
            if report:
                report(f"lost the connection to Docker ({type(exc).__name__}); retrying")
        if last_ts is not None:
            since, skip_until = int(last_ts.timestamp()), last_ts  # whole seconds; duplicates are skipped below
        stop.wait(retry)


def follow_demo(scenario: str, interval: float, put: Put, stop: threading.Event) -> None:
    from .demo import stream_lines

    for line in stream_lines(scenario, interval, stop):
        put(RawLine(line))


# ---------------------------------------------------------------------- the loop


class Watcher:
    def __init__(
        self,
        engine: Engine,
        sources: list[Source],
        *,
        interval: float,
        on_window: Optional[Callable[[WindowResult], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        idle_flush: float = 1.0,
        clock: Callable[[], float] = time.time,
        formats: frozenset = ALL_FORMATS,
    ) -> None:
        self.engine, self.sources, self.interval = engine, sources, interval
        self.on_window, self.on_error = on_window, on_error
        self.idle_flush, self.clock = idle_flush, clock
        self.formats = formats
        self.stop = threading.Event()

    def _inspect(self, source: Source) -> Optional[ContainerInfo]:
        if not source.container:
            return None
        try:
            return inspect_container(source.container)
        except Exception:  # Docker hiccups must not stop the watch
            return None

    def run(self, duration: Optional[float] = None) -> None:
        lines: queue.Queue = queue.Queue()
        for source in self.sources:
            def put(raw: RawLine, name: str = source.name) -> None:
                lines.put((name, raw, self.clock()))

            threading.Thread(target=self._run_reader, args=(source, put), daemon=True).start()

        assemblers = {s.name: Assembler(s.name, formats=self.formats) for s in self.sources}
        records: dict[str, list[LogRecord]] = {s.name: [] for s in self.sources}
        last_line: dict[str, float] = {s.name: 0.0 for s in self.sources}

        start = self.clock()
        deadline = start + duration if duration else None
        while not self.stop.is_set():
            window_end = start + self.interval
            stopping = False
            while True:
                now = self.clock()
                if deadline is not None and now >= deadline:
                    stopping = True
                    window_end = min(window_end, now)
                    break
                if now >= window_end:
                    break
                limit = min(window_end, deadline) if deadline is not None else window_end
                try:
                    name, raw, t = lines.get(timeout=max(0.01, min(limit - now, 0.25)))
                except queue.Empty:
                    if self.stop.is_set():
                        stopping = True
                        window_end = now
                        break
                    continue
                last_line[name] = t
                records[name].extend(assemblers[name].feed(raw))

            for source in self.sources:
                a = assemblers[source.name]
                if a.pending and (stopping or window_end - last_line[source.name] >= self.idle_flush):
                    rec = a.flush()
                    if rec is not None:
                        records[source.name].append(rec)
                if stopping and not records[source.name] and window_end - start < 0.5:
                    continue  # nothing happened in a sliver of a final window
                result = self.engine.process_window(
                    source.name, records[source.name], start, window_end, self._inspect(source)
                )
                records[source.name] = []
                if self.on_window:
                    self.on_window(result)
            if stopping:
                return
            start = window_end

    def _run_reader(self, source: Source, put: Put) -> None:
        try:
            source.reader(put, self.stop)
        except Exception as exc:  # a crashed reader must be visible, not silent
            if self.on_error:
                self.on_error(f"{source.name}: reader stopped ({type(exc).__name__}: {exc})")
