"""Unit tests for the webhook notification channel (roadmap item 23).

Covers the keyring-backed URL storage, the gated send path, and the
guarantee that the toast still fires when webhook delivery fails. All HTTP
is mocked (monkeypatching `requests.post`); no real network, no pystray
loop, no real keyring backend.
"""
from __future__ import annotations

import types

import pytest
import requests

import tray_status as t


# ---------------------------------------------------------------- keyring storage
def test_get_webhook_url_reads_from_keyring(monkeypatch):
    fake = types.ModuleType("keyring")
    fake.get_password = lambda svc, u: (
        "https://ntfy.sh/mytopic" if (svc, u) == (t.KEYRING_SERVICE, t.WEBHOOK_KEYRING_USERNAME)
        else None
    )
    monkeypatch.setattr(t, "keyring", fake)
    assert t.get_webhook_url() == "https://ntfy.sh/mytopic"


def test_get_webhook_url_missing_returns_none(monkeypatch):
    fake = types.ModuleType("keyring")
    fake.get_password = lambda svc, u: None
    monkeypatch.setattr(t, "keyring", fake)
    assert t.get_webhook_url() is None


def test_get_webhook_url_no_backend_returns_none_and_logs(monkeypatch, tmp_path):
    fake = types.ModuleType("keyring")
    def boom(*a, **k):
        raise RuntimeError("no backend")
    fake.get_password = boom
    monkeypatch.setattr(t, "keyring", fake)
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)
    assert t.get_webhook_url() is None
    assert "keyring" in log_path.read_text(encoding="utf-8").lower()


def test_set_webhook_url_stores_in_keyring(monkeypatch):
    store: dict = {}
    fake = types.ModuleType("keyring")
    fake.set_password = lambda svc, u, v: store.__setitem__((svc, u), v)
    monkeypatch.setattr(t, "keyring", fake)
    t.set_webhook_url("https://ntfy.sh/mytopic")
    assert store[(t.KEYRING_SERVICE, t.WEBHOOK_KEYRING_USERNAME)] == "https://ntfy.sh/mytopic"


def test_clear_webhook_url_removes_from_keyring(monkeypatch):
    store = {(t.KEYRING_SERVICE, t.WEBHOOK_KEYRING_USERNAME): "https://ntfy.sh/mytopic"}
    fake = types.ModuleType("keyring")
    fake.delete_password = lambda svc, u: store.pop((svc, u))
    monkeypatch.setattr(t, "keyring", fake)
    t.clear_webhook_url()
    assert (t.KEYRING_SERVICE, t.WEBHOOK_KEYRING_USERNAME) not in store


def test_clear_webhook_url_is_harmless_when_nothing_stored(monkeypatch):
    fake = types.ModuleType("keyring")
    def boom(svc, u):
        raise RuntimeError("not found")
    fake.delete_password = boom
    monkeypatch.setattr(t, "keyring", fake)
    t.clear_webhook_url()  # must not raise


def test_set_webhook_url_keyring_unavailable_logs_and_returns_false(monkeypatch, tmp_path):
    # Mirrors test_get_webhook_url_no_backend_returns_none_and_logs: a
    # locked/unavailable keyring backend must be logged and swallowed, not
    # raised as a traceback out of --set-webhook-url.
    fake = types.ModuleType("keyring")
    def boom(*a, **k):
        raise RuntimeError("no backend")
    fake.set_password = boom
    monkeypatch.setattr(t, "keyring", fake)
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)

    result = t.set_webhook_url("https://ntfy.sh/mytopic")  # must not raise

    assert result is False
    assert "keyring" in log_path.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------- config lookup anchoring
def test_load_tray_config_reads_config_toml_regardless_of_cwd(monkeypatch, tmp_path):
    # The tray can be launched from any CWD (a pythonw shortcut, a Task
    # Scheduler job at logon), so the config.toml lookup must anchor to
    # SCRIPT_DIR, not the process's current working directory -- otherwise
    # [alerts] webhook_enabled silently stays False with no symptom.
    script_dir = tmp_path / "install"
    script_dir.mkdir()
    (script_dir / "config.toml").write_text(
        "[alerts]\nwebhook_enabled = true\n", encoding="utf-8",
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setattr(t, "SCRIPT_DIR", script_dir)
    monkeypatch.chdir(elsewhere)
    # Register the module global for monkeypatch's automatic restore,
    # regardless of how the code under test mutates it.
    monkeypatch.setattr(t.pcss_config, "WEBHOOK_ENABLED", t.pcss_config.WEBHOOK_ENABLED)

    t._load_tray_config()

    assert t.pcss_config.WEBHOOK_ENABLED is True


# ---------------------------------------------------------------- send_webhook payload
def test_send_webhook_posts_plain_text_body(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return types.SimpleNamespace(ok=True, status_code=200)

    monkeypatch.setattr(t.requests, "post", fake_post)
    t.send_webhook("https://ntfy.sh/mytopic", "voltage_anomalies=2", timeout=5.0)

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://ntfy.sh/mytopic"
    assert kwargs["data"] == b"voltage_anomalies=2"
    assert kwargs["timeout"] == 5.0
    assert kwargs["headers"]["Content-Type"] == "text/plain; charset=utf-8"


def test_send_webhook_non_2xx_is_logged_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(
        t.requests, "post",
        lambda url, **kw: types.SimpleNamespace(ok=False, status_code=500),
    )
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)
    t.send_webhook("https://example.com/hook", "text")  # must not raise
    assert "500" in log_path.read_text(encoding="utf-8")


def test_send_webhook_timeout_is_swallowed_and_logged(monkeypatch, tmp_path):
    def raise_timeout(url, **kw):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr(t.requests, "post", raise_timeout)
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)

    t.send_webhook("https://example.com/hook", "text", timeout=5.0)  # must not raise

    logged = log_path.read_text(encoding="utf-8")
    assert "Webhook" in logged
    assert "Timeout" in logged
    # The secret-bearing URL must never be written to the log.
    assert "example.com" not in logged


def test_send_webhook_connection_error_is_swallowed_and_logged(monkeypatch, tmp_path):
    def raise_conn_error(url, **kw):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(t.requests, "post", raise_conn_error)
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)

    t.send_webhook("https://example.com/hook", "text")  # must not raise
    assert "ConnectionError" in log_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- gating
def test_maybe_send_webhook_noop_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(t.requests, "post", lambda *a, **k: calls.append(1))
    t.maybe_send_webhook(False, "https://example.com/hook", "text")
    assert calls == []


def test_maybe_send_webhook_noop_when_url_missing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(t.requests, "post", lambda *a, **k: calls.append(1))
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)
    t.maybe_send_webhook(True, None, "text")
    assert calls == []
    assert "no url" in log_path.read_text(encoding="utf-8").lower() \
        or "no-op" in log_path.read_text(encoding="utf-8").lower() \
        or "skip" in log_path.read_text(encoding="utf-8").lower()


def test_maybe_send_webhook_sends_when_enabled_and_url_present(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return types.SimpleNamespace(ok=True, status_code=200)

    monkeypatch.setattr(t.requests, "post", fake_post)
    t.maybe_send_webhook(True, "https://example.com/hook", "text")
    assert calls == ["https://example.com/hook"]


# ---------------------------------------------------------------- toast still fires
class _FakeIcon:
    def __init__(self):
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message, title=None):
        self.notifications.append((message, title))


class _ImmediateThread:
    """Runs the target synchronously instead of on a real OS thread, so the
    test can assert ordering/isolation deterministically without a sleep or
    join."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def test_notify_alert_toasts_even_when_webhook_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(t.threading, "Thread", _ImmediateThread)

    def raise_error(url, **kw):
        raise requests.exceptions.RequestException("boom")

    monkeypatch.setattr(t.requests, "post", raise_error)
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)

    icon = _FakeIcon()
    # Must not raise, and the toast must have fired despite the webhook error.
    t.notify_alert(icon, "voltage_anomalies=2", True, "https://example.com/hook")

    assert icon.notifications
    assert icon.notifications[0][0] == "voltage_anomalies=2"


def test_notify_alert_skips_webhook_thread_when_disabled(monkeypatch):
    spawned = []

    class _CountingThread(_ImmediateThread):
        def start(self):
            spawned.append(1)
            super().start()

    monkeypatch.setattr(t.threading, "Thread", _CountingThread)
    monkeypatch.setattr(t.requests, "post", lambda *a, **k: pytest.fail("must not be called"))

    icon = _FakeIcon()
    t.notify_alert(icon, "some alert", False, None)

    assert icon.notifications
    # The gate now runs before the thread spawn: a fully-disabled webhook
    # channel does no extra work at all -- no thread, no daemon overhead.
    assert spawned == []


def test_notify_alert_skips_webhook_thread_when_enabled_but_url_missing(monkeypatch, tmp_path):
    spawned = []

    class _CountingThread(_ImmediateThread):
        def start(self):
            spawned.append(1)
            super().start()

    monkeypatch.setattr(t.threading, "Thread", _CountingThread)
    monkeypatch.setattr(t.requests, "post", lambda *a, **k: pytest.fail("must not be called"))
    log_path = tmp_path / "tray_status.log"
    monkeypatch.setattr(t, "TRAY_LOG", log_path)

    icon = _FakeIcon()
    t.notify_alert(icon, "some alert", True, None)

    assert icon.notifications
    assert spawned == []
    logged = log_path.read_text(encoding="utf-8").lower()
    assert "no url" in logged or "no-op" in logged or "skip" in logged


# ---------------------------------------------------------------- multi-line delivery (finding 1)
def test_two_lines_appended_between_polls_are_both_notified_in_order(tmp_path, monkeypatch):
    # A single analyzer run can append two lines to alerts.log: an
    # event-driven anomaly alert (_maybe_write_alerts) and, separately, the
    # weekly digest (_maybe_write_weekly_digest). AlertWatcher.poll() used to
    # return only the last new line, so whenever both fired in the same run
    # the anomaly toast was silently swallowed. The caller must notify once
    # per line polled, in order.
    monkeypatch.setattr(t.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        t.requests, "post",
        lambda url, **kw: types.SimpleNamespace(ok=True, status_code=200),
    )

    p = tmp_path / "alerts.log"
    p.write_text("", encoding="utf-8")
    watcher = t.AlertWatcher(p, cooldown_sec=0)
    with p.open("a", encoding="utf-8") as f:
        f.write("2026-07-06 10:00:00  voltage_anomalies=2\n")
        f.write("2026-07-06 10:00:00  weekly_digest  period=1.00 kWh\n")

    icon = _FakeIcon()
    for alert in watcher.poll(now=1000.0):
        t.notify_alert(icon, alert, False, None)

    assert len(icon.notifications) == 2
    assert "voltage_anomalies=2" in icon.notifications[0][0]
    assert "weekly_digest" in icon.notifications[1][0]


# ---------------------------------------------------------------- CLI flags
def test_parse_cli_args_set_webhook_url():
    args = t.parse_cli_args(["--set-webhook-url"])
    assert args.set_webhook_url is True
    assert args.clear_webhook_url is False


def test_parse_cli_args_clear_webhook_url():
    args = t.parse_cli_args(["--clear-webhook-url"])
    assert args.clear_webhook_url is True
    assert args.set_webhook_url is False


def test_parse_cli_args_defaults_to_neither():
    args = t.parse_cli_args([])
    assert args.set_webhook_url is False
    assert args.clear_webhook_url is False
