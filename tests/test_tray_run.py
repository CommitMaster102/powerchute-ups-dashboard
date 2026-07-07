"""Unit tests for the "run the analyzer from the tray" helpers.

Covers the pure decision logic that backs the "Actualizar dashboard" menu
item: command construction with a venv fallback, the --no-snapshot decision
read from scheduled_run.ps1's once-a-day marker, single-flight state
transitions, and log-tail truncation for the failure toast. None of this
spawns pystray or a real subprocess.
"""
from __future__ import annotations

import subprocess
import sys

import tray_status as t


# ---------------------------------------------------------------- single-flight
def test_single_flight_claims_and_releases():
    gate = t.SingleFlightRun()
    assert gate.active is False
    assert gate.try_start() is True
    assert gate.active is True


def test_single_flight_rejects_second_claim_while_active():
    gate = t.SingleFlightRun()
    assert gate.try_start() is True
    # A second claim while the first is still active is rejected outright.
    assert gate.try_start() is False
    assert gate.active is True


def test_single_flight_allows_new_run_after_finish():
    gate = t.SingleFlightRun()
    gate.try_start()
    gate.finish()
    assert gate.active is False
    assert gate.try_start() is True


def test_single_flight_finish_without_start_is_harmless():
    gate = t.SingleFlightRun()
    gate.finish()  # no-op, must not raise
    assert gate.active is False


# ---------------------------------------------------------------- --no-snapshot decision
def test_marker_reports_today_matches_exact_date():
    assert t.marker_reports_today("2026-07-06", "2026-07-06") is True


def test_marker_reports_today_mismatched_date():
    assert t.marker_reports_today("2026-07-05", "2026-07-06") is False


def test_marker_reports_today_tolerates_trailing_newline():
    # scheduled_run.ps1 writes the marker with Set-Content, which appends a
    # line ending; the comparison must not choke on it.
    assert t.marker_reports_today("2026-07-06\r\n", "2026-07-06") is True
    assert t.marker_reports_today("2026-07-06\n", "2026-07-06") is True


def test_marker_reports_today_only_looks_at_first_line():
    assert t.marker_reports_today("2026-07-06\nignored extra content", "2026-07-06") is True


def test_marker_reports_today_missing_or_empty_content():
    assert t.marker_reports_today(None, "2026-07-06") is False
    assert t.marker_reports_today("", "2026-07-06") is False
    assert t.marker_reports_today("   ", "2026-07-06") is False


def test_wants_no_snapshot_true_when_marker_file_records_today(tmp_path):
    marker = tmp_path / "last_scheduled_run.txt"
    marker.write_text("2026-07-06\n", encoding="utf-8")
    assert t.wants_no_snapshot(marker, "2026-07-06") is True


def test_wants_no_snapshot_false_when_marker_records_a_different_day(tmp_path):
    marker = tmp_path / "last_scheduled_run.txt"
    marker.write_text("2026-07-05\n", encoding="utf-8")
    assert t.wants_no_snapshot(marker, "2026-07-06") is False


def test_wants_no_snapshot_false_when_marker_file_is_missing(tmp_path):
    marker = tmp_path / "does_not_exist.txt"
    assert t.wants_no_snapshot(marker, "2026-07-06") is False


def test_wants_no_snapshot_false_when_marker_file_is_not_utf8(tmp_path):
    """A marker file with invalid UTF-8 bytes raises UnicodeDecodeError from
    read_text, not OSError — wants_no_snapshot's docstring promises a
    decision, not a crash, so this must fall back to "run a full snapshot"
    like a missing file rather than propagating (polish item A5a)."""
    marker = tmp_path / "last_scheduled_run.txt"
    marker.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    assert t.wants_no_snapshot(marker, "2026-07-06") is False


# ---------------------------------------------------------------- command construction
def test_analyzer_command_uses_venv_python_when_present(tmp_path):
    venv_py = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")  # only needs to exist

    cmd = t.analyzer_command(tmp_path, no_snapshot=False)

    assert cmd[0] == str(venv_py)
    assert cmd[1:] == ["analyze_ups.py", "--no-browser", "--quiet"]


def test_analyzer_command_falls_back_to_sys_executable(tmp_path):
    # No .venv under tmp_path, so the venv interpreter does not exist.
    cmd = t.analyzer_command(tmp_path, no_snapshot=False)
    assert cmd[0] == sys.executable


def test_analyzer_command_appends_no_snapshot_flag(tmp_path):
    cmd = t.analyzer_command(tmp_path, no_snapshot=True)
    assert cmd[-1] == "--no-snapshot"
    assert cmd[1:] == ["analyze_ups.py", "--no-browser", "--quiet", "--no-snapshot"]


def test_analyzer_command_omits_no_snapshot_flag_by_default(tmp_path):
    cmd = t.analyzer_command(tmp_path, no_snapshot=False)
    assert "--no-snapshot" not in cmd


# ---------------------------------------------------------------- log-tail truncation
def test_tail_of_text_keeps_last_n_nonblank_lines():
    text = "\n".join(f"line {i}" for i in range(1, 11))
    result = t.tail_of_text(text, max_lines=3, max_chars=1000)
    assert result == "line 8\nline 9\nline 10"


def test_tail_of_text_skips_blank_lines():
    text = "line 1\n\n\nline 2\n\nline 3\n"
    result = t.tail_of_text(text, max_lines=2, max_chars=1000)
    assert result == "line 2\nline 3"


def test_tail_of_text_truncates_to_max_chars_keeping_the_end():
    text = "a" * 50 + "\n" + "b" * 50
    result = t.tail_of_text(text, max_lines=10, max_chars=20)
    assert len(result) == 20
    assert result == ("b" * 50)[-20:]


def test_tail_of_text_empty_input():
    assert t.tail_of_text("", max_lines=5, max_chars=100) == ""


def test_tail_of_log_reads_and_tails_existing_file(tmp_path):
    log_path = tmp_path / "tray_run.log"
    log_path.write_text("ok line\nERROR: boom\n", encoding="utf-8")
    result = t.tail_of_log(log_path, max_lines=1, max_chars=100)
    assert result == "ERROR: boom"


def test_tail_of_log_missing_file_returns_placeholder(tmp_path):
    log_path = tmp_path / "does_not_exist.log"
    result = t.tail_of_log(log_path, max_lines=5, max_chars=100)
    assert result  # non-empty placeholder, never raises


# ---------------------------------------------------------------- watchdog timeout
class _FakeProc:
    """A stand-in for a spawned analyzer process. Its first wait raises
    TimeoutExpired (a wedged run); any later wait returns a code."""

    def __init__(self, wait_results):
        self.pid = 4321
        self.killed = False
        self.wait_timeouts: list = []
        self._results = list(wait_results)

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def kill(self):
        self.killed = True


class _FakeIcon:
    def __init__(self):
        self.notes: list[tuple] = []

    def notify(self, message, title=None):
        self.notes.append((message, title))


def test_tray_run_timeout_constant_is_fifteen_minutes():
    assert t.TRAY_RUN_TIMEOUT_SEC == 15 * 60


def test_kill_process_tree_kills_and_never_raises(monkeypatch):
    run_calls = []
    monkeypatch.setattr(t.subprocess, "run", lambda *a, **k: run_calls.append((a, k)))
    proc = _FakeProc([0])
    t._kill_process_tree(proc)
    assert proc.killed is True
    # A process-tree kill was attempted (taskkill names the pid).
    assert any("taskkill" in str(a) and "4321" in str(a) for a, k in run_calls)


def test_kill_process_tree_swallows_all_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("no taskkill here")

    monkeypatch.setattr(t.subprocess, "run", boom)

    class Explosive:
        pid = 1
        def kill(self):
            raise RuntimeError("already gone")

    t._kill_process_tree(Explosive())  # must not raise


def _patch_worker_env(monkeypatch, tmp_path):
    monkeypatch.setattr(t, "TRAY_RUN_LOG", tmp_path / "tray_run.log")
    monkeypatch.setattr(t, "SCHEDULED_RUN_MARKER", tmp_path / "marker.txt")
    monkeypatch.setattr(t, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(t, "log", lambda *a, **k: None)
    monkeypatch.setattr(t.subprocess, "run", lambda *a, **k: None)  # taskkill no-op


def test_worker_timeout_kills_releases_gate_and_toasts(monkeypatch, tmp_path):
    _patch_worker_env(monkeypatch, tmp_path)
    fake = _FakeProc([subprocess.TimeoutExpired(cmd="analyze", timeout=t.TRAY_RUN_TIMEOUT_SEC), 1])
    monkeypatch.setattr(t.subprocess, "Popen", lambda *a, **k: fake)
    icon = _FakeIcon()
    gate = t.SingleFlightRun()
    assert gate.try_start() is True

    t._run_analyzer_worker(icon, gate)

    # The wait was bounded by the watchdog timeout, not an unbounded wait().
    assert fake.wait_timeouts[0] == t.TRAY_RUN_TIMEOUT_SEC
    assert fake.killed is True                 # the wedged process was killed
    assert gate.active is False                # the single-flight slot was released
    assert icon.notes, "a failure toast must fire on timeout"
    body = icon.notes[-1][0].lower()
    assert "min" in body or "límite" in body or "cancel" in body


def test_worker_success_path_still_toasts_ok(monkeypatch, tmp_path):
    _patch_worker_env(monkeypatch, tmp_path)
    fake = _FakeProc([0])
    monkeypatch.setattr(t.subprocess, "Popen", lambda *a, **k: fake)
    icon = _FakeIcon()
    gate = t.SingleFlightRun()
    gate.try_start()

    t._run_analyzer_worker(icon, gate)

    assert fake.wait_timeouts[0] == t.TRAY_RUN_TIMEOUT_SEC
    assert fake.killed is False
    assert gate.active is False
    assert any("actualizado" in msg.lower() for msg, _ in icon.notes)
