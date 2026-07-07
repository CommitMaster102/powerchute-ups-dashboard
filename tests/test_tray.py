"""Unit tests for tray_status pure functions (pytest, no PCSS server, no GUI).

Covers locale parsing, clamping, color thresholds, icon rendering, status-HTML
parsing, and the keyring password resolution/migration (with a fake backend).
"""
from __future__ import annotations

import sys
import types

import pytest

import tray_status as t


# ---------------------------------------------------------------- locale parse
@pytest.mark.parametrize("raw,expected", [
    ("54,0", 54.0),
    ("54.0", 54.0),
    ("54", 54.0),
    ("88 %", 88.0),
    ("100%", 100.0),
    ("", None),
    ("abc", None),
])
def test_parse_locale_number(raw, expected):
    assert t._parse_locale_number(raw) == expected


# ---------------------------------------------------------------- clamp / color
@pytest.mark.parametrize("n,expected", [(150, 100), (-5, 0), (50, 50), (0, 0), (100, 100)])
def test_clamp_pct(n, expected):
    assert t._clamp_pct(n) == expected


@pytest.mark.parametrize("pct,rgba", [
    (None, (160, 160, 160, 255)),
    (75, (40, 200, 80, 255)),    # green >= 60
    (45, (255, 180, 0, 255)),    # amber >= 30
    (10, (220, 60, 60, 255)),    # red < 30
])
def test_color_for_pct(pct, rgba):
    assert t.color_for_pct(pct) == rgba


# ---------------------------------------------------------------- icon render
@pytest.mark.parametrize("pct", [0, 42, 100, None])
def test_render_icon(pct):
    img = t.render_icon(pct)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"


# ---------------------------------------------------------------- status parse
SAMPLE_HTML = """
<html><body>
  <div id="value_BatteryCharge">88</div>
  <div id="value_RealPowerPct">19,5</div>
  <div id="value_RuntimeRemaining">42</div>
  <div id="value_InputVoltage">122,0</div>
  <div id="value_VoltageDC">27,4</div>
  <div id="value_DeviceStatus">En línea</div>
</body></html>
"""


def test_parse_status_extracts_fields():
    s = t.parse_status(SAMPLE_HTML)
    assert s.battery_pct == 88
    assert s.load_pct == pytest.approx(19.5)
    assert s.runtime_min == 42
    assert s.input_v == pytest.approx(122.0)
    assert s.battery_v == pytest.approx(27.4)
    assert s.device_status == "En línea"


def test_parse_status_clamps_battery():
    s = t.parse_status('<div id="value_BatteryCharge">150</div>')
    assert s.battery_pct == 100


# ---------------------------------------------------------------- keyring
def test_resolve_password_migrates_then_reads(tmp_path, monkeypatch):
    store: dict = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda svc, u: store.get((svc, u))
    fake.set_password = lambda svc, u, p: store.__setitem__((svc, u), p)
    monkeypatch.setitem(sys.modules, "keyring", fake)
    monkeypatch.setattr(t, "keyring", fake)

    cf = tmp_path / "credentials.txt"
    cf.write_text("username = bob\npassword = secret123\nurl = x\n", encoding="utf-8")
    monkeypatch.setattr(t, "CREDENTIALS_FILE", cf)

    # First call migrates the file password into the keyring and blanks the file.
    assert t._resolve_password("bob", "secret123") == "secret123"
    assert store[(t.KEYRING_SERVICE, "bob")] == "secret123"
    assert "secret123" not in cf.read_text()

    # Second call (file blanked) reads it back from the keyring.
    assert t._resolve_password("bob", "") == "secret123"


def test_resolve_password_falls_back_without_backend(monkeypatch):
    fake = types.ModuleType("keyring")
    def boom(*a, **k):
        raise RuntimeError("no backend")
    fake.get_password = boom
    fake.set_password = boom
    monkeypatch.setattr(t, "keyring", fake)
    # No usable backend -> use the file password as-is.
    assert t._resolve_password("bob", "filepw") == "filepw"


# ---------------------------------------------------------------- already-connected
@pytest.mark.parametrize("html,expected", [
    ("El usuario ya está conectado", True),
    ("usuario ya conectado en otra sesión", True),
    ("No se ha podido iniciar la sesión", True),
    ("User already connected", True),
    ("Bienvenido — sesión iniciada", False),
    ("", False),
])
def test_is_already_connected(html, expected):
    assert t._is_already_connected(html) is expected


# ---------------------------------------------------------------- alert watcher
def test_alert_watcher_starts_at_end_of_file(tmp_path):
    p = tmp_path / "alerts.log"
    p.write_text("old alert line\n", encoding="utf-8")
    w = t.AlertWatcher(p, cooldown_sec=0)
    assert w.poll(now=1000.0) == []                       # history never re-notifies
    with p.open("a", encoding="utf-8") as f:
        f.write("2026-07-06 10:00:00  voltage_anomalies=2\n")
    lines = w.poll(now=1001.0)
    assert len(lines) == 1
    assert "voltage_anomalies=2" in lines[0]
    assert w.poll(now=1002.0) == []                       # nothing new


def test_alert_watcher_delivers_every_new_line_in_one_poll(tmp_path):
    # A single analyzer run can append two lines in the same run (an
    # event-driven anomaly alert, then the weekly digest) — both must be
    # delivered, in order, not just the last one (the bug this test guards
    # against: poll() used to return only lines[-1], silently dropping the
    # anomaly line whenever the digest also fired the same run).
    p = tmp_path / "alerts.log"
    p.write_text("", encoding="utf-8")
    w = t.AlertWatcher(p, cooldown_sec=0)
    with p.open("a", encoding="utf-8") as f:
        f.write("2026-07-06 10:00:00  voltage_anomalies=2\n")
        f.write("2026-07-06 10:00:00  weekly_digest  period=1.00 kWh\n")
    lines = w.poll(now=1000.0)
    assert len(lines) == 2
    assert "voltage_anomalies=2" in lines[0]
    assert "weekly_digest" in lines[1]


def test_alert_watcher_cooldown_swallows_repeats(tmp_path):
    p = tmp_path / "alerts.log"
    p.write_text("", encoding="utf-8")
    w = t.AlertWatcher(p, cooldown_sec=1800)
    with p.open("a", encoding="utf-8") as f:
        f.write("first\n")
    assert w.poll(now=1000.0) == ["first"]
    with p.open("a", encoding="utf-8") as f:
        f.write("second\n")
    # Inside the cooldown the new line is consumed without notifying.
    assert w.poll(now=1100.0) == []
    with p.open("a", encoding="utf-8") as f:
        f.write("third\n")
    # After the cooldown expires the next new line notifies again.
    assert w.poll(now=1000.0 + 1801) == ["third"]


def test_alert_watcher_handles_truncation(tmp_path):
    p = tmp_path / "alerts.log"
    p.write_text("a long line of history\n" * 5, encoding="utf-8")
    w = t.AlertWatcher(p, cooldown_sec=0)
    p.write_text("fresh\n", encoding="utf-8")            # rotated / truncated
    assert w.poll(now=1000.0) == ["fresh"]


def test_alert_watcher_missing_file(tmp_path):
    p = tmp_path / "alerts.log"
    w = t.AlertWatcher(p, cooldown_sec=0)
    assert w.poll(now=1000.0) == []
    p.write_text("born later\n", encoding="utf-8")
    assert w.poll(now=1001.0) == ["born later"]


# ---------------------------------------------------------------- TLS hardening
def test_client_pins_cert_and_ignores_env():
    # PCSSClient must not trust ambient REQUESTS_CA_BUNDLE / proxy env: the
    # pinned self-signed cert is the only source of trust (audit hardening).
    c = t.PCSSClient("https://localhost:6547", "u", "p")
    assert c.session.trust_env is False
    assert c._host == "localhost"
    assert c._port == 6547


def test_parse_locale_number_decimal_comma_and_dot():
    # Documents current behaviour: a single comma is the decimal sep (Spanish);
    # a bare integer and an English decimal both parse.
    assert t._parse_locale_number("27,4") == pytest.approx(27.4)
    assert t._parse_locale_number("1234.5") == pytest.approx(1234.5)
    assert t._parse_locale_number("100") == pytest.approx(100.0)
