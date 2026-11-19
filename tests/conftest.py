import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

GOOD_REPLY = json.dumps({
    "overall": "The database at <IP_1> is unreachable, so every request fails.",
    "patterns": [
        {
            "pattern": 1,
            "summary": "The app cannot connect to its database.",
            "likely_cause": "The database server is down or refusing connections.",
            "confidence": "high",
            "evidence": ["ECONNREFUSED <IP_1>:5432"],
            "things_to_check": ["docker ps -a", "docker logs --tail 50 db 2>&1", "docker restart db", "rm -rf /var/lib/db"],
        },
        {
            "pattern": 2,
            "summary": "Requests fail with a database traceback.",
            "likely_cause": "Same root cause as pattern 1.",
            "confidence": "medium",
            "evidence": [],
            "things_to_check": [],
        },
    ],
})


@dataclass
class Recorded:
    path: str
    headers: dict
    body: dict


@dataclass
class FakeLLM:
    url: str = ""
    requests: list = field(default_factory=list)
    replies: list = field(default_factory=list)  # consumed one per request, then default_reply
    statuses: list = field(default_factory=list)  # consumed one per request, then 200
    default_reply: str = GOOD_REPLY
    delay: float = 0.0
    host_port: str = ""


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # clients that hang up early are expected
        pass


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        fake: FakeLLM = self.server.fake
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        fake.requests.append(Recorded(self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if fake.delay:
            time.sleep(fake.delay)
        status = fake.statuses.pop(0) if fake.statuses else 200
        if status != 200:
            return self._send(status, {"error": {"message": "simulated failure"}})
        reply = fake.replies.pop(0) if fake.replies else fake.default_reply
        if self.path == "/api/chat":
            return self._send(200, {"message": {"role": "assistant", "content": reply}})
        if self.path.endswith("/chat/completions"):
            return self._send(200, {"choices": [{"message": {"content": reply}}]})
        if self.path == "/v1/messages":
            return self._send(200, {"content": [{"type": "text", "text": reply}]})
        return self._send(404, {"error": "unknown path"})


@pytest.fixture
def fake_llm():
    server = _QuietServer(("127.0.0.1", 0), _Handler)
    fake = FakeLLM()
    server.fake = fake
    port = server.server_address[1]
    fake.url = f"http://127.0.0.1:{port}"
    fake.host_port = f"127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake
    server.shutdown()
    server.server_close()
