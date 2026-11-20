import json

import pytest

from strelizia.notify import (
    Discord, File, Message, Notifier, NotifyError, Ntfy, Slack, Telegram, Webhook,
    parse_target, parse_targets,
)

MSG = Message(title="api: 1 new error pattern", text="NEW · ERROR · 12x\n  ERROR boom <IP>", level="ERROR",
              alerts=[{"kind": "NEW", "count": 12}])


def test_url_detection():
    assert isinstance(parse_target("https://discord.com/api/webhooks/1/abc"), Discord)
    assert isinstance(parse_target("https://discordapp.com/api/webhooks/1/abc"), Discord)
    assert isinstance(parse_target("https://hooks.slack.com/services/T/B/x"), Slack)
    assert isinstance(parse_target("telegram://123:ABC/-100200"), Telegram)
    assert isinstance(parse_target("ntfy://my-topic"), Ntfy)
    assert isinstance(parse_target("ntfy://ntfy.example.com/alerts"), Ntfy)
    assert isinstance(parse_target("https://ntfy.sh/my-topic"), Ntfy)
    assert isinstance(parse_target("https://example.com/hook"), Webhook)
    assert isinstance(parse_target("file://alerts.jsonl"), File)


@pytest.mark.parametrize("bad", ["", "ftp://x", "telegram://onlytoken", "ntfy://", "file://", "not a url", "discord"])
def test_bad_urls_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_target(bad)


@pytest.mark.parametrize("bad", [
    "telegram://SECRETTOKEN",                  # missing chat id
    "ftp://user:SECRETTOKEN@host/x",           # unsupported scheme
    "ntfy://a/b/c/SECRETTOKEN",                # too many parts
    "SECRETTOKEN",                             # not a URL at all
])
def test_errors_for_bad_urls_never_echo_secrets(bad):
    with pytest.raises(ValueError) as exc:
        parse_target(bad)
    assert "SECRETTOKEN" not in str(exc.value)
    with pytest.raises(ValueError) as exc2:
        parse_targets([bad], env={})
    assert "SECRETTOKEN" not in str(exc2.value)


def test_labels_never_contain_secrets():
    for url in ["https://discord.com/api/webhooks/1/SECRETTOKEN", "https://hooks.slack.com/services/T/B/SECRETTOKEN",
                "telegram://123:SECRETTOKEN/55", "https://example.com/hook?key=SECRETTOKEN"]:
        assert "SECRET" not in parse_target(url).label


def test_environment_variable_targets(tmp_path):
    env = {"STRELIZIA_NOTIFY": f"file://{tmp_path}/a.jsonl, ntfy://topic"}
    notifiers = parse_targets(["https://example.com/hook"], env=env)
    assert [n.label for n in notifiers] == ["webhook", "file", "ntfy"]
    assert parse_targets([], env={}) == []
    with pytest.raises(ValueError):
        parse_targets([], env={"STRELIZIA_NOTIFY": "bogus"})


def test_discord_payload(fake_hook):
    Discord(fake_hook.url + "/api/webhooks/1/x").send(MSG)
    (req,) = fake_hook.requests
    assert req.body["content"].startswith("**api: 1 new error pattern**")
    assert "ERROR boom <IP>" in req.body["content"]


def test_discord_respects_the_2000_character_limit(fake_hook):
    Discord(fake_hook.url + "/x").send(Message("t", "x" * 5000))
    assert len(fake_hook.requests[0].body["content"]) <= 2000


def test_slack_payload(fake_hook):
    Slack(fake_hook.url + "/services/T/B/x").send(MSG)
    assert fake_hook.requests[0].body["text"].startswith("*api: 1 new error pattern*")


def test_telegram_payload_and_custom_api(fake_hook):
    parse_target(f"telegram://123:ABC/-100200?api={fake_hook.url}").send(MSG)
    (req,) = fake_hook.requests
    assert req.path == "/bot123:ABC/sendMessage"
    assert req.body["chat_id"] == "-100200" and "ERROR boom" in req.body["text"]


def test_ntfy_headers_are_ascii_and_prioritised(fake_hook):
    host = fake_hook.url.removeprefix("http://")
    parse_target(f"ntfy+http://{host}/alerts").send(Message("🔥 api: new error", "body 🔥", level="FATAL"))
    (req,) = fake_hook.requests
    assert req.path == "/alerts"
    assert req.headers["title"] == " api: new error".lstrip() or req.headers["title"].isascii()
    assert req.headers["priority"] == "5"
    assert req.body == "body 🔥"


def test_generic_webhook_gets_structured_json(fake_hook):
    Webhook(fake_hook.url + "/hook").send(MSG)
    body = fake_hook.requests[0].body
    assert body["title"] == MSG.title and body["alerts"] == [{"kind": "NEW", "count": 12}]


def test_file_notifier_appends_json_lines(tmp_path):
    path = tmp_path / "sub" / "alerts.jsonl"
    n = parse_target(f"file://{path}")
    n.send(MSG)
    n.send(MSG)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["title"] == MSG.title


def test_file_notifier_reports_unwritable_paths(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(NotifyError, match="file: could not write"):
        File(str(blocker / "child.jsonl")).send(MSG)


def test_http_errors_do_not_leak_the_url(fake_hook):
    fake_hook.status = 400
    secret_url = fake_hook.url + "/api/webhooks/1/SECRETTOKEN"
    with pytest.raises(NotifyError) as exc:
        Discord(secret_url).send(MSG)
    assert "400" in str(exc.value) and "SECRETTOKEN" not in str(exc.value)
    assert len(fake_hook.requests) == 1  # client errors are not retried


def test_server_errors_are_retried_once(fake_hook):
    fake_hook.status = 503
    with pytest.raises(NotifyError, match="503"):
        Webhook(fake_hook.url + "/x").send(MSG)
    assert len(fake_hook.requests) == 2


def test_unreachable_host_does_not_leak_the_url():
    with pytest.raises(NotifyError) as exc:
        Webhook("http://127.0.0.1:9/SECRETTOKEN").send(MSG)
    assert "SECRETTOKEN" not in str(exc.value) and "could not connect" in str(exc.value)
