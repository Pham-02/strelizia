from strelizia.demo import generate
from strelizia.redact import Redactor


def red(text):
    r = Redactor()
    return r.redact(text), r


def test_ips_get_stable_placeholders_and_keep_the_port():
    out, r = red("connect 10.0.0.5:5432 failed; retry 10.0.0.5:5432; client 203.0.113.7")
    assert out == "connect <IP_1>:5432 failed; retry <IP_1>:5432; client <IP_2>"
    assert r.counts() == {"IP": 2}


def test_not_an_ip():
    out, _ = red("value 1000.1.1.1 and version 2.4.1")
    assert out == "value 1000.1.1.1 and version 2.4.1"


def test_ipv6_but_not_timestamps():
    out, _ = red("peer fe80::1ff:fe23:4567:890a and 2001:db8::1 and ::1 at 14:02:11 on 2026-11-18T14:02:11")
    assert "fe80" not in out and "2001:db8" not in out and "::1" not in out
    assert "14:02:11" in out and "2026-11-18T14:02:11" in out


def test_email():
    out, _ = red("login failed for jane.doe+test@example.co.uk")
    assert "jane" not in out and "<EMAIL_1>" in out


def test_known_token_formats():
    secrets = [
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_" + "a1B2c3D4e5F6" * 3,
        "xoxb-123456789012-abcdefghijkl",
        "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
        "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
    ]
    for s in secrets:
        out, _ = red(f"calling api with {s} now")
        assert s not in out, s
        assert "<TOKEN_1>" in out, s


def test_url_credentials():
    out, _ = red("db at postgres://admin:s3cr3tP4ss@db.internal:5432/app")
    assert "s3cr3tP4ss" not in out and "admin" not in out
    assert "postgres://<CREDENTIAL_1>@" in out


def test_authorization_header():
    out, _ = red("Authorization: Bearer abc123.def456.ghi789-xyz")
    assert "abc123" not in out and "Bearer <TOKEN_1>" in out


def test_key_value_secrets():
    out, _ = red('password=hunter2xyz api_key: "Zx81QmN0pLk" token=expired')
    assert "hunter2xyz" not in out and "Zx81QmN0pLk" not in out
    assert "token=expired" in out  # plain word, kept readable


def test_private_key_block():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabc\n-----END RSA PRIVATE KEY-----"
    out, _ = red(f"loaded {pem} ok")
    assert "MIIE" not in out and "<PRIVATE_KEY_1>" in out


def test_usernames_in_paths():
    out, _ = red("open /home/alice/app/config.yml and C:\\Users\\Bob\\app.log")
    assert "alice" not in out and "Bob" not in out
    assert "/home/<USER_1>/app/config.yml" in out


def test_generic_high_entropy_token():
    out, _ = red("session k3J9xQ2mZ8vB1nL5pR7tY4uW6e started")
    assert "k3J9xQ2mZ8vB1nL5pR7tY4uW6e" not in out


def test_normal_log_text_is_left_alone():
    text = (
        "java.lang.NullPointerException: Cannot invoke String.length()\n"
        "\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:883)\n"
        'File "/usr/lib/python3/psycopg2/__init__.py", line 122, in connect\n'
        "psycopg2.OperationalError: Connection refused\nWARN retrying in 4s (attempt 8)"
    )
    out, r = red(text)
    assert out == text
    assert r.counts() == {}


def test_redaction_is_idempotent():
    once, _ = red("from 10.0.0.5 token=Zx81QmN0pLk user a@b.com")
    twice, _ = red(once)
    assert once == twice


def test_demo_logs_only_lose_ips():
    r = Redactor()
    for scenario in ("db-outage", "nginx-502", "bad-deploy"):
        for line in generate(scenario)[:400]:
            r.redact(line)
    assert set(r.counts()) <= {"IP"}


def test_summary_text():
    _, r = red("a 1.2.3.4 b 5.6.7.8 c x@y.com")
    assert r.summary() == "1 email, 2 ips"
    assert Redactor().summary() == "nothing needed masking"


def test_web_routes_are_not_usernames():
    text = "GET /api/users/42 and /Users-list and /home/dashboard HTTP/1.1"
    out, r = red(text)
    assert out == text and r.counts() == {}
