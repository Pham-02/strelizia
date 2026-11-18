"""Read logs from a Docker container (stdout and stderr, merged)."""

from __future__ import annotations

import re
import time
from typing import Iterator, Optional

from ..assembler import parse_timestamp
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


def read_container(
    name: str, since_seconds: Optional[int] = None, tail: Optional[int] = None
) -> Iterator[RawLine]:
    """Yield the container's recent log lines."""
    try:
        import docker
        from docker.errors import DockerException, NotFound
    except ImportError:  # pragma: no cover
        raise SourceError("The 'docker' package is not installed (pip install docker)") from None

    try:
        client = docker.from_env()
        container = client.containers.get(name)
        kwargs: dict = {"stdout": True, "stderr": True, "timestamps": True}
        if since_seconds:
            kwargs["since"] = int(time.time() - since_seconds)
        if tail:
            kwargs["tail"] = tail
        data: bytes = container.logs(**kwargs)
    except NotFound:
        raise SourceError(f"No container named or with id {name!r}. Check `docker ps -a`.") from None
    except DockerException as exc:
        raise SourceError(
            f"Could not talk to Docker ({exc}). Is the Docker daemon running and are you "
            "allowed to access it?"
        ) from None

    for line in data.decode("utf-8", errors="replace").splitlines():
        yield split_docker_prefix(line)
