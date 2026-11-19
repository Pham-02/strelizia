"""Read logs from a Docker container (stdout and stderr, merged)."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterator, Optional

from ..util import parse_timestamp
from ..models import RawLine
from . import SourceError

# `docker logs --timestamps` prefixes EVERY physical line with an RFC3339 timestamp.
_DOCKER_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s?(.*)$")


def split_docker_prefix(line: str) -> RawLine:
    """Separate the Docker timestamp from the application's own line."""
    m = _DOCKER_PREFIX.match(line)
    if not m:
        return RawLine(line)
    return RawLine(m.group(2), parse_timestamp(m.group(1)))


@dataclass
class ContainerInfo:
    """Facts about a container that often explain its logs (restarts, OOM kills)."""

    name: str
    image: str
    status: str
    restart_count: int
    oom_killed: bool
    exit_code: Optional[int]
    started_at: str
    finished_at: str

    def facts(self) -> list[str]:
        out = [
            f"container: {self.name} (image {self.image})",
            f"container status: {self.status}",
            f"container restart count: {self.restart_count}",
            f"container was killed for running out of memory: {'yes' if self.oom_killed else 'no'}",
        ]
        if self.started_at:
            out.append(f"container last started: {self.started_at}")
        if self.finished_at:
            out.append(f"container last stopped: {self.finished_at}")
        if self.exit_code not in (None, 0):
            out.append(f"container last exit code: {self.exit_code}")
        return out


def _get_container(name: str):
    try:
        import docker
        from docker.errors import DockerException, NotFound
    except ImportError:  # pragma: no cover
        raise SourceError("The 'docker' package is not installed (pip install docker)") from None
    try:
        return docker.from_env().containers.get(name)
    except NotFound:
        raise SourceError(f"No container named or with id {name!r}. Check `docker ps -a`.") from None
    except DockerException as exc:
        raise SourceError(
            f"Could not talk to Docker ({exc}). Is the Docker daemon running and are you "
            "allowed to access it?"
        ) from None


def inspect_container(name: str) -> ContainerInfo:
    attrs = _get_container(name).attrs
    state = attrs.get("State", {})
    finished = state.get("FinishedAt", "") or ""
    return ContainerInfo(
        name=attrs.get("Name", name).lstrip("/"),
        image=attrs.get("Config", {}).get("Image", "unknown"),
        status=state.get("Status", "unknown"),
        restart_count=int(attrs.get("RestartCount", 0) or 0),
        oom_killed=bool(state.get("OOMKilled", False)),
        exit_code=state.get("ExitCode"),
        started_at=state.get("StartedAt", "") or "",
        finished_at="" if finished.startswith("0001-") else finished,
    )


def read_container(
    name: str, since_seconds: Optional[int] = None, tail: Optional[int] = None
) -> Iterator[RawLine]:
    """Yield the container's recent log lines."""
    container = _get_container(name)
    kwargs: dict = {"stdout": True, "stderr": True, "timestamps": True}
    if since_seconds:
        kwargs["since"] = int(time.time() - since_seconds)
    if tail:
        kwargs["tail"] = tail
    data: bytes = container.logs(**kwargs)
    for line in data.decode("utf-8", errors="replace").splitlines():
        yield split_docker_prefix(line)
