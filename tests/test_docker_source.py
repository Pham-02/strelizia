import docker
import docker.errors
import pytest

from strelizia.sources import SourceError
from strelizia.sources.docker import read_container


class FakeContainer:
    def __init__(self):
        self.kwargs = None

    def logs(self, **kwargs):
        self.kwargs = kwargs
        return (
            b"2026-11-18T14:02:11.100000000Z ERROR request failed\n"
            b"2026-11-18T14:02:11.100000001Z Traceback (most recent call last):\n"
            b'2026-11-18T14:02:11.100000002Z   File "/app/x.py", line 3, in f\n'
            b"2026-11-18T14:02:11.100000003Z ValueError: bad\n"
        )


class FakeClient:
    def __init__(self, container=None, error=None):
        self._container, self._error = container, error
        self.containers = self

    def get(self, name):
        if self._error:
            raise self._error
        return self._container


def test_reads_and_strips_docker_timestamps(monkeypatch):
    fake = FakeContainer()
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(fake))
    lines = list(read_container("api", since_seconds=3600))
    assert [ln.text for ln in lines][0] == "ERROR request failed"
    assert all(ln.ts is not None for ln in lines)
    assert fake.kwargs["timestamps"] is True
    assert fake.kwargs["stdout"] and fake.kwargs["stderr"]
    assert "since" in fake.kwargs


def test_container_not_found_is_friendly(monkeypatch):
    monkeypatch.setattr(docker, "from_env", lambda: FakeClient(error=docker.errors.NotFound("x")))
    with pytest.raises(SourceError, match="docker ps -a"):
        list(read_container("nope"))


def test_daemon_down_is_friendly(monkeypatch):
    def boom():
        raise docker.errors.DockerException("Error while fetching server API version")

    monkeypatch.setattr(docker, "from_env", boom)
    with pytest.raises(SourceError, match="Docker daemon"):
        list(read_container("api"))
