"""Email alerts: leadgen/mail.py sends the owner an email (from the site's problem reports
and from the GitHub workflows), on its own with the standard library only. No test ever
reaches a mail server: smtplib's SMTP and SMTP_SSL are replaced by a stand-in."""

import ast
import logging
import os
import pathlib
import re
import subprocess
import sys

import pytest

from leadgen import alerts, cli, config, health, mail, web

ROOT = pathlib.Path(__file__).resolve().parent.parent
USER = "sender@example.com"
PASSWORD = "app-password-not-real"


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL: notes what the site did with it."""
    made: list["FakeSMTP"] = []
    fail_login: Exception | None = None

    def __init__(self, host, port, timeout=None, context=None, ssl=False):
        self.host, self.port, self.timeout, self.ssl = host, port, timeout, ssl
        self.context, self.tls, self.login_as, self.messages = context, False, None, []
        FakeSMTP.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if FakeSMTP.fail_login is not None:
            raise FakeSMTP.fail_login
        self.login_as = (user, password)

    def send_message(self, message):
        self.messages.append(message)


@pytest.fixture(autouse=True)
def fresh_status():
    health.forget()
    yield
    health.forget()


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.made, FakeSMTP.fail_login = [], None
    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", lambda *a, **kw: FakeSMTP(*a, ssl=True, **kw))
    monkeypatch.setenv(mail.USER_ENV, USER)
    monkeypatch.setenv(mail.PASSWORD_ENV, PASSWORD)
    return FakeSMTP


def _sent(smtp):
    return [m for server in smtp.made for m in server.messages]


# ---- mail.py on its own

def test_mail_uses_nothing_from_the_leadgen_package():
    tree = ast.parse((ROOT / "leadgen" / "mail.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("leadgen"), node.module
            assert (node.module or "").split(".")[0] in sys.stdlib_module_names, node.module
        elif isinstance(node, ast.Import):
            assert all(a.name.split(".")[0] in sys.stdlib_module_names for a in node.names)
    assert config.ALERT_EMAIL == mail.ALERT_EMAIL == "t@thomasewright.com"


def test_the_script_without_settings_prints_a_notice_and_exits_0():
    """As the workflows run it: a plain python3, from the repository, no packages needed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEADGEN_")}
    done = subprocess.run([sys.executable, "-S", "leadgen/mail.py", "--subject", "Lead Finder: CI failed on main",
                           "body"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ("No email sent: Email is not set up (LEADGEN_SMTP_USER and "
                                   "LEADGEN_SMTP_PASSWORD are not both set).")


def test_sends_with_starttls_and_login_to_the_owner(smtp, capsys):
    assert mail.main(["--subject", "Lead Finder: CI failed on main", "Commit: abc", "Run: https://x"]) == 0
    [server] = smtp.made
    assert (server.host, server.port, server.timeout, server.ssl) == ("smtp.gmail.com", 587, 15, False)
    assert server.tls and server.login_as == (USER, PASSWORD)
    [message] = server.messages
    assert message["To"] == "t@thomasewright.com" and message["From"] == USER
    assert message["Subject"] == "Lead Finder: CI failed on main"
    assert message.get_content().strip() == "Commit: abc\nRun: https://x"
    out = capsys.readouterr().out
    assert out.strip() == "Emailed t@thomasewright.com." and PASSWORD not in out and USER not in out


def test_a_gmail_app_password_pasted_with_spaces_still_works(smtp, monkeypatch):
    """Google shows an app password as four groups of four letters; for Gmail the spaces
    are taken out. Another server gets the password exactly as set."""
    monkeypatch.setenv(mail.PASSWORD_ENV, " abcd efgh ijkl mnop ")
    mail.send("s", "b")
    assert smtp.made[-1].login_as == (USER, "abcdefghijklmnop")
    monkeypatch.setenv(mail.HOST_ENV, "smtp.example.net")
    mail.send("s", "b")
    assert smtp.made[-1].login_as == (USER, " abcd efgh ijkl mnop ")
    monkeypatch.delenv(mail.HOST_ENV)
    FakeSMTP.fail_login = RuntimeError("bad login abcdefghijklmnop")
    with pytest.raises(RuntimeError) as failed:
        mail.send("s", "b")
    assert "abcdefghijklmnop" not in str(failed.value) and "***" in str(failed.value)


def test_the_recipient_server_and_sender_can_be_changed(smtp, monkeypatch):
    monkeypatch.setenv(mail.TO_ENV, "someone@example.org")
    monkeypatch.setenv(mail.HOST_ENV, "smtp.example.net")
    monkeypatch.setenv(mail.PORT_ENV, "465")
    monkeypatch.setenv(mail.FROM_ENV, "alerts@example.net")
    assert mail.send("Subject", "Body") == "someone@example.org"
    [server] = smtp.made
    assert server.ssl and not server.tls and (server.host, server.port) == ("smtp.example.net", 465)
    assert server.login_as == (USER, PASSWORD)
    [message] = server.messages
    assert message["To"] == "someone@example.org" and message["From"] == "alerts@example.net"


def test_a_failed_send_exits_1_and_never_shows_the_password_or_user(smtp, capsys):
    smtp.fail_login = mail.smtplib.SMTPAuthenticationError(
        535, f"5.7.8 Username and Password not accepted for {USER} with {PASSWORD}".encode())
    assert mail.main(["--subject", "x", "y"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("The email could not be sent: SMTPAuthenticationError: ")
    assert "Username and Password not accepted" in captured.err
    for text in (captured.out, captured.err):
        assert PASSWORD not in text and USER not in text


# ---- the site's problem reports go by email and to the webhook

@pytest.fixture
def webhook(monkeypatch):
    posted = []
    monkeypatch.setattr(alerts, "ON", True)
    monkeypatch.setenv(alerts.WEBHOOK_ENV, "https://hooks.example.com/abc")
    monkeypatch.setattr(alerts, "_post", lambda url, message: posted.append(message))
    return posted


def test_a_problem_goes_by_email_and_to_the_webhook(smtp, webhook):
    alerts.report("search", "Today's search failed: Couldn't reach the map data")
    [message] = _sent(smtp)
    assert message["Subject"] == "Lead Finder problem: Today's search failed: Couldn't reach the map data"
    assert message.get_content().startswith(f"{alerts.SITE}: Today's search failed")
    assert len(webhook) == 1 and webhook[0] == message.get_content().strip()
    assert alerts.recent()["count"] == 1
    assert health.status()["alerts"]["last_ok"]


def test_the_subject_holds_the_first_80_characters(smtp, monkeypatch):
    monkeypatch.setattr(alerts, "ON", True)
    alerts.report("error", "x" * 200)
    [message] = _sent(smtp)
    assert message["Subject"] == "Lead Finder problem: " + "x" * 80


def test_a_failing_channel_does_not_stop_the_other(smtp, webhook, monkeypatch, caplog):
    smtp.fail_login = OSError("mail server down")
    with caplog.at_level(logging.WARNING):
        alerts.report("error", "First")
    assert len(webhook) == 1 and "Emailing the problem failed: OSError: mail server down" in caplog.text
    status = health.status()["alerts"]
    assert status["failure_reason"] == "Email: OSError: mail server down"

    smtp.fail_login = None
    health.forget()

    def refuse(url, message):
        raise ConnectionError("webhook down")
    monkeypatch.setattr(alerts, "_post", refuse)
    with caplog.at_level(logging.WARNING):
        alerts.report("error", "Second")
    assert [m["Subject"] for m in _sent(smtp)] == ["Lead Finder problem: Second"]
    assert "Sending the problem to LEADGEN_ALERT_WEBHOOK failed: ConnectionError" in caplog.text


def test_at_most_ten_alert_emails_an_hour(smtp, monkeypatch, caplog):
    monkeypatch.setattr(alerts, "ON", True)
    clock = [1_000_000.0]
    monkeypatch.setattr(alerts.time, "time", lambda: clock[0])
    with caplog.at_level(logging.WARNING):
        for n in range(12):
            alerts.report("error", f"Problem {n}")
            clock[0] += 60
    assert len(_sent(smtp)) == alerts.EMAILS_PER_HOUR == 10
    assert caplog.text.count("Not emailing the problem: 10 alert emails went out in the last hour") == 2
    clock[0] += 3600                     # an hour later, emails go out again
    alerts.report("error", "Problem after an hour")
    assert len(_sent(smtp)) == 11


def test_send_test_with_email_only(smtp, capsys):
    assert cli.main(["test-alert"]) == 0
    out = capsys.readouterr().out
    assert "Delivered by email to t@thomasewright.com." in out and "webhook" not in out
    [message] = _sent(smtp)
    assert message["Subject"] == "Lead Finder: test alert"
    assert "Test alert sent from the command line at " in message.get_content()


def test_send_test_says_where_it_went_and_where_it_failed(smtp, webhook, capsys):
    smtp.fail_login = OSError("mail server down")
    assert cli.main(["test-alert"]) == 2
    captured = capsys.readouterr()
    assert "Delivered to the webhook (LEADGEN_ALERT_WEBHOOK)." in captured.out
    assert "Not delivered by email: OSError: mail server down" in captured.err
    assert PASSWORD not in captured.err + captured.out


def test_the_admin_test_alert_says_it_was_emailed(smtp):
    client = web.create_app(password="s3cret", username="Matt", admin_password="open-sesame").test_client()
    client.post("/login", data={"username": "Matt", "password": "s3cret"})
    client.post("/admin/unlock", json={"password": "open-sesame"})
    page = client.post("/admin/test-alert").get_data(as_text=True)
    assert "Delivered by email to t@thomasewright.com." in page and len(_sent(smtp)) == 1


def test_healthz_counts_email_alone_as_set_up(smtp):
    body = web.create_app().test_client().get("/healthz").get_json()
    assert body["sources"]["alerts"]["set_up"] is True
    text = str(body)
    assert USER not in text and PASSWORD not in text
    assert health.LABELS["alerts"] == "Alerts (email or webhook)"


def test_check_sources_names_the_email_channel(smtp):
    from leadgen import checks
    row = checks._alerts()
    assert row.set_up and row.detail.startswith("Set up (email to t@thomasewright.com)")


# ---- the GitHub workflows email the owner when they fail, and the settings are documented

def _jobs(workflow):
    """{job name: its text} of a workflow file (read as text: CI installs no YAML reader)."""
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _check_notice(notice):
    assert "LEADGEN_SMTP_USER: ${{ secrets.LEADGEN_SMTP_USER }}" in notice
    assert "LEADGEN_SMTP_PASSWORD: ${{ secrets.LEADGEN_SMTP_PASSWORD }}" in notice
    assert 'python3 leadgen/mail.py --subject "Lead Finder: $WORKFLOW failed on $BRANCH"' in notice
    assert "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}" in notice
    assert "$COMMIT" in notice and "$PUSHED_BY" in notice and "$RUN_URL" in notice
    # Nothing a pusher controls is pasted into the script itself, only passed in env.
    script = notice.split("run: |", 1)[1]
    assert "${{" not in script


@pytest.mark.parametrize("name", ["backup.yml", "restore-rehearsal.yml"])
def test_each_workflow_emails_the_owner_when_it_fails(name):
    jobs = _jobs((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    notice = jobs.pop("email-on-failure")
    needs = re.search(r"^    needs: \[([^\]]+)\]$", notice, re.M).group(1)
    assert sorted(n.strip() for n in needs.split(",")) == sorted(jobs)
    assert re.search(r"^    if: failure\(\)$", notice, re.M)
    assert re.findall(r"uses: (\S+)", notice) == ["actions/checkout@v4"]       # nothing third-party
    _check_notice(notice)


def test_ci_emails_the_owner_from_a_last_step_so_a_green_run_skips_no_check():
    """Render deploys only when every check passed; a skipped notice job held a deploy back
    (2026-10-08), so in CI the email is each job's last step instead."""
    jobs = _jobs((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert sorted(jobs) == ["lint", "test"]
    for name, job in jobs.items():
        last = job.split("\n      - ")[-1]
        assert last.startswith("name: Email the owner\n        if: failure()\n"), name
        assert "uses:" not in last
        _check_notice(last)


def test_the_email_settings_are_in_render_and_the_example_file():
    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    for key in ("LEADGEN_SMTP_USER", "LEADGEN_SMTP_PASSWORD"):
        assert re.search(rf"- key: {key}\b[^\n]*\n\s+sync: false", render), key
    example = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for key in ("LEADGEN_SMTP_USER", "LEADGEN_SMTP_PASSWORD", "LEADGEN_ALERT_EMAIL"):
        assert f"{key}=" in example and example[example.index(f"{key}=") - 1].startswith("# ")
    runbook = " ".join((ROOT / "docs" / "operator-runbook.md").read_text(encoding="utf-8").split())
    assert "App passwords" in runbook and "t@thomasewright.com" in runbook
    assert "`LEADGEN_SMTP_USER`" in runbook and "`LEADGEN_SMTP_PASSWORD`" in runbook
