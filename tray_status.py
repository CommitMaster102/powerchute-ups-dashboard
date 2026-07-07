"""
PCSS battery tray icon.

Polls https://localhost:6547/status periodically, logs in automatically using
the credentials in credentials.txt, and shows the UPS battery percentage as a
dynamic system-tray icon (battery silhouette + number, color-coded).

Right-click menu:
  - Current % (info, disabled)
  - Open PCSS web UI
  - Open local dashboard (analyze_ups.py output)
  - Update dashboard (runs analyze_ups.py in the background, then toasts)
  - Refresh now
  - Exit

To run silently (no console), use run_tray.bat (which calls pythonw.exe).
For autostart, drop a shortcut to run_tray.bat into:
  shell:startup     (Win+R, type that, paste the shortcut)
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import keyring
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from pystray import Icon, Menu, MenuItem

from pcss import config as pcss_config

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_FILE = SCRIPT_DIR / "credentials.txt"
OUTPUT = SCRIPT_DIR / "output"
OUTPUT.mkdir(exist_ok=True)
DEBUG_DUMP_HTML = OUTPUT / "status_raw.html"
TRAY_LOG = OUTPUT / "tray_status.log"
# PCSS serves a self-signed cert on localhost. Instead of disabling TLS
# verification globally, we trust-on-first-use: save the server's cert here and
# verify against it. Delete this file to force a re-pin (e.g. after a PCSS
# reinstall changes the cert).
PCSS_CERT = OUTPUT / "pcss_cert.pem"
# OS keyring (Windows Credential Manager) service name for the PCSS password.
KEYRING_SERVICE = "stateOfUPS-PCSS"
# Dedicated keyring entry (under the same service) for the webhook URL
# (roadmap item 23). It is stored under this "username" purely to reuse the
# keyring's (service, username) -> secret shape; it is not an actual account.
WEBHOOK_KEYRING_USERNAME = "webhook-url"
# Scratch log for a tray-triggered analyzer run (item 24) — overwritten on
# every run, not history like tray_status.log or the scheduled-run log.
TRAY_RUN_LOG = OUTPUT / "tray_run.log"
# Watchdog ceiling for one tray-triggered analyzer run (item 24). A healthy
# run finishes in seconds; this generous cap keeps a hung analyzer from
# wedging the single-flight slot until the tray is restarted. On expiry the
# process tree is killed, a failure toast fires, and the slot is released.
TRAY_RUN_TIMEOUT_SEC = 15 * 60
# The once-a-day marker scheduled_run.ps1 writes on a successful run; read
# here (never written) to decide whether a tray-triggered run needs the
# snapshot or can pass --no-snapshot.
SCHEDULED_RUN_MARKER = OUTPUT / "last_scheduled_run.txt"

CREDENTIALS_TEMPLATE = """\
# PowerChute Serial Shutdown credentials.
#
# Put your password here ONCE: on the next run it is moved into the Windows
# Credential Manager (keyring) and this line is blanked automatically. After
# that, only the username/url/poll live here. PCSS listens on localhost with a
# self-signed cert which the tray pins on first use (output/pcss_cert.pem).
# Do NOT commit, sync, or share this file.
#
# Lines beginning with '#' are comments. Format: key = value.

username =
password =
url = https://localhost:6547
poll_interval_sec = 60
"""

DEFAULTS = {
    "url": "https://localhost:6547",
    "poll_interval_sec": "60",
    "username": "",
    "password": "",
}


# ----------------------------------------------------------------------
# Logging (file-only, since pythonw has no console)
# ----------------------------------------------------------------------
def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        with TRAY_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def msgbox(text: str, title: str = "PCSS tray", icon: int = 0x40) -> None:
    """Windows MessageBox (no extra deps). icon: 0x40=info, 0x30=warn, 0x10=err."""
    with contextlib.suppress(Exception):
        ctypes.windll.user32.MessageBoxW(0, text, title, icon)


# ----------------------------------------------------------------------
# Single-instance lock (named mutex, per-user-session)
# ----------------------------------------------------------------------
_INSTANCE_MUTEX_NAME = "Local\\PCSSTrayStatus_v1_singleton"
_ERROR_ALREADY_EXISTS = 183


_PRIVATE_BROWSERS = [
    # (exe filename, list of common install paths, private-mode flag)
    ("chrome.exe",
     [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
     "--incognito"),
    ("msedge.exe",
     [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
      r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
     "--inprivate"),
    ("brave.exe",
     [r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
      r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"],
     "--incognito"),
    ("firefox.exe",
     [r"C:\Program Files\Mozilla Firefox\firefox.exe",
      r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"],
     "-private-window"),
]


def _open_private_window(url: str) -> bool:
    """Launch `url` in the first browser found, in private/incognito mode."""
    import shutil
    import subprocess
    for exe, paths, flag in _PRIVATE_BROWSERS:
        for p in paths:
            if Path(p).exists():
                try:
                    subprocess.Popen([p, flag, url],
                                     creationflags=0x08000000,  # NO_WINDOW
                                     close_fds=True)
                    log(f"Launched {exe} {flag} {url[-60:]}")
                    return True
                except Exception as e:
                    log(f"Launch {p} failed: {e}")
        full = shutil.which(exe)
        if full:
            try:
                subprocess.Popen([full, flag, url],
                                 creationflags=0x08000000,
                                 close_fds=True)
                log(f"Launched (PATH) {exe} {flag}")
                return True
            except Exception as e:
                log(f"Launch via PATH {exe} failed: {e}")
    return False


def acquire_single_instance() -> int | None:
    """Return a non-zero HANDLE if we are the first instance, else None.
    Caller must keep the handle alive for the lifetime of the process."""
    try:
        h = ctypes.windll.kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        if not h:
            return None
        if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(h)
            return None
        return h
    except Exception:
        return None


# ----------------------------------------------------------------------
# Credentials
# ----------------------------------------------------------------------
def load_credentials() -> dict:
    if not CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.write_text(CREDENTIALS_TEMPLATE, encoding="utf-8")
        msgbox(
            f"Created credentials template at:\n\n{CREDENTIALS_FILE}\n\n"
            "Edit it with your PCSS username and password, then run again.",
            "PCSS tray — first-time setup",
        )
        raise SystemExit(0)

    cfg = dict(DEFAULTS)
    for raw in CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()

    # Password comes from the OS keyring; the plaintext file is only a
    # migration source / fallback (see _resolve_password).
    cfg["password"] = _resolve_password(cfg.get("username", ""), cfg.get("password", ""))

    if not cfg["username"] or not cfg["password"]:
        msgbox(
            f"Credentials incomplete.\n\nSet 'username' in:\n{CREDENTIALS_FILE}\n\n"
            "and the password either in that file (it will be migrated to the\n"
            "Windows Credential Manager on next run) or directly in the keyring\n"
            f"under service '{KEYRING_SERVICE}'. Then run again.",
            "PCSS tray — credentials missing",
        )
        raise SystemExit(1)

    cfg["poll_interval_sec"] = str(max(10, int(cfg["poll_interval_sec"])))
    return cfg


def _resolve_password(username: str, file_pw: str) -> str:
    """Prefer the OS keyring; migrate a file password into it once, then blank
    the plaintext copy. Falls back to the file if no keyring backend exists."""
    if not username:
        return file_pw
    try:
        kr_pw = keyring.get_password(KEYRING_SERVICE, username)
    except Exception as e:  # no usable backend
        log(f"keyring unavailable ({e}); using credentials.txt password.")
        return file_pw
    if kr_pw:
        return kr_pw
    if file_pw:
        try:
            keyring.set_password(KEYRING_SERVICE, username, file_pw)
            _blank_password_in_file(CREDENTIALS_FILE)
            log("Migrated PCSS password to the OS keyring; blanked credentials.txt.")
        except Exception as e:
            log(f"keyring store failed ({e}); keeping the file password.")
        return file_pw
    return ""


def _blank_password_in_file(path: Path) -> None:
    """Replace the `password = ...` line with an empty value (post-migration)."""
    lines = path.read_text(encoding="utf-8").splitlines()
    out = [
        "password =" if (ln.strip().lower().startswith("password") and "=" in ln) else ln
        for ln in lines
    ]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ----------------------------------------------------------------------
# PCSS client
# ----------------------------------------------------------------------
def _pin_cert(host: str, port: int, path: Path = PCSS_CERT) -> Path:
    """Trust-on-first-use: fetch the server's (self-signed) cert and save it."""
    pem = ssl.get_server_certificate((host, port))
    path.write_text(pem, encoding="utf-8")
    return path


class PCSSClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.user = username
        self.passwd = password
        self.session = requests.Session()
        # We only ever talk to localhost with a pinned self-signed cert, so the
        # pinned PEM must be the sole source of trust. trust_env=False stops a
        # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / proxy env var (common in
        # corporate setups) from silently overriding session.verify and making
        # the pinned cert look invalid.
        self.session.trust_env = False
        parsed = urlparse(self.base)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 6547
        self._logged_in = False

    def _ensure_verify(self) -> None:
        """Pin the cert on first use and verify against it. If pinning fails
        (e.g. the server is unreachable), let the error propagate rather than
        falling back to verify=False: an unverified connection opens a MitM
        window, and on requests<2.32 (CVE-2024-35195) the first verify=False
        request poisons the connection pool so later pinned requests stay
        unverified for the life of the session. A failed pin surfaces as a
        normal connection error that refresh() catches and shows as an error
        icon — the next poll retries the pin cleanly."""
        if not PCSS_CERT.exists():
            _pin_cert(self._host, self._port)
            log(f"Pinned PCSS cert -> {PCSS_CERT}")
        self.session.verify = str(PCSS_CERT)

    def _request(self, method: str, url: str, **kw):
        """Wraps session.request with cert pinning + one re-pin retry on TLS
        failure (e.g. PCSS reinstalled with a new self-signed cert)."""
        self._ensure_verify()
        try:
            return self.session.request(method, url, **kw)
        except requests.exceptions.SSLError:
            log("TLS verify failed; re-pinning PCSS cert and retrying once.")
            with contextlib.suppress(FileNotFoundError):
                PCSS_CERT.unlink()
            self._ensure_verify()
            return self.session.request(method, url, **kw)

    def login(self) -> None:
        log("Logging in to PCSS...")
        r = self._request("GET", f"{self.base}/logon", allow_redirects=True, timeout=10)
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        formtoken_input = soup.find("input", attrs={"name": "formtoken"})
        formtokenid_input = soup.find("input", attrs={"name": "formtokenid"})
        formtoken = formtoken_input["value"] if formtoken_input else ""
        formtokenid = formtokenid_input["value"] if formtokenid_input else "/logon_formtoken"

        form = soup.find("form", attrs={"name": "loginForm"}) or soup.find("form")
        action = form["action"] if form and form.has_attr("action") else "j_security_check"
        action = re.sub(r";jsessionid=[^?]*", "", str(action))
        if not action.startswith("http"):
            action = f"{self.base}/{action.lstrip('/')}"

        r = self._request(
            "POST",
            action,
            data={
                "j_username": self.user,
                "j_password": self.passwd,
                "formtoken": formtoken,
                "formtokenid": formtokenid,
                "login": "Inicio de sesion",
            },
            allow_redirects=True,
            timeout=10,
        )
        r.raise_for_status()

        # PCSS returns 200 with the login page (and an error message) on rejection.
        # Distinguish "user already connected" from generic auth failure.
        if "loginForm" in r.text or r.url.rstrip("/").endswith("/logon"):
            if _is_already_connected(r.text):
                raise UserAlreadyConnected(
                    "Otra sesión tiene esta cuenta abierta")
            raise RuntimeError("Login failed (credentials rejected by PCSS)")

        # Belt-and-suspenders: confirm /status is reachable
        check = self._request("GET", f"{self.base}/status", allow_redirects=False, timeout=10)
        if check.status_code in (302, 303):
            loc = check.headers.get("Location", "?")
            raise RuntimeError(f"Login failed (redirected to {loc} after auth)")

        self._logged_in = True
        log("Login OK.")

    def _fetch_status(self) -> str:
        r = self._request("GET", f"{self.base}/status", timeout=10, allow_redirects=True)
        r.raise_for_status()
        # If the response is the login page, our session expired
        if "loginForm" in r.text or "j_security_check" in r.text:
            raise SessionExpired()
        return r.text

    def get_status(self) -> PcssStatus:
        if not self._logged_in:
            self.login()
        try:
            html = self._fetch_status()
        except SessionExpired:
            self._logged_in = False
            self.login()
            html = self._fetch_status()
        with contextlib.suppress(Exception):
            DEBUG_DUMP_HTML.write_text(html, encoding="utf-8")
        return parse_status(html)


class SessionExpired(Exception):
    pass


class UserAlreadyConnected(Exception):
    """PCSS rejected login because another session has the same user logged in.
    Typically: the user opened the PCSS web UI and is using the session there."""
    pass


_BUSY_POLL_INTERVAL_SEC = 15   # while busy, retry this often so we recover fast
_ALREADY_CONNECTED_MARKERS = (
    "ya está conectado", "ya esta conectado",
    "usuario ya", "already connected",
    "no se ha podido iniciar la sesión",
    "no se ha podido iniciar la sesion",
)


def _is_already_connected(html: str) -> bool:
    h = html.lower()
    return any(m in h for m in _ALREADY_CONNECTED_MARKERS)


# ----------------------------------------------------------------------
# Run the analyzer from the tray (item 24)
# ----------------------------------------------------------------------
class SingleFlightRun:
    """Tracks whether a tray-triggered analyzer run is already active.

    At most one tray-triggered run is ever wanted, so a second click while
    one is in flight is rejected outright rather than queued. `try_start`
    and `finish` are the only state transitions, and both take the same
    lock so concurrent clicks from the pystray callback thread cannot race.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def try_start(self) -> bool:
        """Claim the slot. Returns True if this call claimed it, False if a
        run is already active."""
        with self._lock:
            if self._active:
                return False
            self._active = True
            return True

    def finish(self) -> None:
        """Release the slot. Safe to call even when no run was active."""
        with self._lock:
            self._active = False


def analyzer_command(script_dir: Path, no_snapshot: bool) -> list[str]:
    """Build the argv for a tray-triggered analyzer run.

    Prefers the project's own virtualenv interpreter
    (`script_dir/.venv/Scripts/python.exe`), the same interpreter
    `scheduled_run.ps1` uses, and falls back to `sys.executable` when that
    venv does not exist. The analyzer path is left relative (`analyze_ups.py`)
    because the caller runs the process with `cwd=script_dir`, matching
    `scheduled_run.ps1`'s own invocation.
    """
    venv_python = script_dir / ".venv" / "Scripts" / "python.exe"
    python = str(venv_python) if venv_python.exists() else sys.executable
    cmd = [python, "analyze_ups.py", "--no-browser", "--quiet"]
    if no_snapshot:
        cmd.append("--no-snapshot")
    return cmd


def marker_reports_today(marker_text: str | None, today: str) -> bool:
    """True when the once-a-day marker already records `today`.

    Matches the format `scheduled_run.ps1` writes: `Set-Content -Value
    $today`, where `$today` is `(Get-Date).ToString('yyyy-MM-dd')` — a
    single line, with whatever line ending `Set-Content` adds. `marker_text`
    is the raw file content (or None when the file does not exist yet), and
    `today` is injected rather than read from the wall clock so the decision
    stays testable.
    """
    if not marker_text or not marker_text.strip():
        return False
    first_line = marker_text.splitlines()[0].strip()
    return first_line == today


def wants_no_snapshot(marker_path: Path, today: str) -> bool:
    """Read the scheduled-run marker and decide whether this tray-triggered
    run should pass --no-snapshot (the marker already records `today`) or
    run a full snapshot run, substituting for a missed scheduled run. Reuses
    `marker_reports_today` rather than duplicating the threshold logic."""
    try:
        text: str | None = marker_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        text = None
    return marker_reports_today(text, today)


def tail_of_text(text: str, max_lines: int = 6, max_chars: int = 300) -> str:
    """Return the last few non-blank lines of `text`, trimmed to `max_chars`
    (keeping the end, since that is where the error usually is) so the
    result fits a Windows toast body."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    tail = lines[-max_lines:] if max_lines > 0 else lines
    result = "\n".join(tail)
    if max_chars > 0 and len(result) > max_chars:
        result = result[-max_chars:]
    return result


def tail_of_log(log_path: Path, max_lines: int = 6, max_chars: int = 300) -> str:
    """Read `log_path` and return its tail via `tail_of_text`. A missing or
    unreadable log yields a placeholder instead of raising, since this feeds
    a failure toast and must never itself crash the caller."""
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(no se pudo leer el log de la ejecución)"
    return tail_of_text(text, max_lines=max_lines, max_chars=max_chars)


def _kill_process_tree(proc) -> None:
    """Terminate a spawned analyzer process and any children it started.

    A plain proc.kill() ends only the direct child on Windows, so a wedged
    analyzer that itself spawned a helper could leave that helper running.
    taskkill /F /T /PID walks the whole tree; a plain proc.kill() is the
    fallback when taskkill is unavailable or the process is already gone.
    This runs on the watchdog path, where the caller is already handling a
    timeout, so it swallows every error rather than raising a second problem.
    """
    pid = getattr(proc, "pid", None)
    if pid is not None:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                creationflags=0x08000000,  # NO_WINDOW
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30,
            )
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=10)


def _run_analyzer_worker(icon: Icon, gate: SingleFlightRun) -> None:
    """Runs on a background thread: spawns the analyzer, waits for it under a
    watchdog timeout, and reports completion via a toast. This is the thin
    pystray-facing wiring around the pure helpers above; the timeout path,
    the process-tree kill, and the gate release are covered by mocked-Popen
    tests in tests/test_tray_run.py.
    """
    try:
        today = time.strftime("%Y-%m-%d")
        no_snapshot = wants_no_snapshot(SCHEDULED_RUN_MARKER, today)
        cmd = analyzer_command(SCRIPT_DIR, no_snapshot)
        log(f"Actualizar dashboard: ejecutando {cmd!r} (no_snapshot={no_snapshot}).")
        code: int | None = None
        timed_out = False
        with TRAY_RUN_LOG.open("w", encoding="utf-8") as f:
            proc = subprocess.Popen(
                cmd,
                cwd=str(SCRIPT_DIR),
                stdout=f,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000,  # NO_WINDOW
                close_fds=True,
            )
            try:
                code = proc.wait(timeout=TRAY_RUN_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                # A hung analyzer must not wedge the single-flight slot until
                # the tray restarts: kill the tree and report the failure.
                _kill_process_tree(proc)
                timed_out = True
        if timed_out:
            tail = tail_of_log(TRAY_RUN_LOG)
            minutes = TRAY_RUN_TIMEOUT_SEC // 60
            icon.notify(
                f"La actualización superó el límite de {minutes} min y fue "
                f"cancelada:\n{tail}", "PCSS tray")
            log(f"Actualizar dashboard: timeout tras {TRAY_RUN_TIMEOUT_SEC}s; "
                f"proceso terminado.\n{tail}")
        elif code == 0:
            icon.notify("Dashboard actualizado.", "PCSS tray")
            log("Actualizar dashboard: terminó OK.")
        else:
            tail = tail_of_log(TRAY_RUN_LOG)
            icon.notify(f"Error ({code}) actualizando el dashboard:\n{tail}", "PCSS tray")
            log(f"Actualizar dashboard: código de salida {code}.\n{tail}")
    except Exception as e:
        log(f"Actualizar dashboard: excepción {e}\n{traceback.format_exc()}")
        with contextlib.suppress(Exception):
            icon.notify(f"Error actualizando el dashboard: {e}", "PCSS tray")
    finally:
        gate.finish()


def run_analyzer_now(icon: Icon, gate: SingleFlightRun) -> None:
    """Menu handler for "Actualizar dashboard". A second click while a run
    is active is rejected with a toast (single-flight) rather than
    disabling the menu item, since every other item here stays static. The
    subprocess wait always happens on a background thread so the pystray
    loop never blocks.
    """
    if not gate.try_start():
        icon.notify("Ya se está actualizando el dashboard.", "PCSS tray")
        return
    threading.Thread(target=_run_analyzer_worker, args=(icon, gate), daemon=True).start()


# ----------------------------------------------------------------------
# Alert watching
# ----------------------------------------------------------------------
class AlertWatcher:
    """Tails the analyzer's alerts.log and reports new lines for a tray toast.

    Windows toasts from a scheduled, non-interactive task are unreliable, so
    the notification lives here: the tray is a long-lived interactive process
    and simply watches the file the analyzer appends to (config [alerts]
    enabled). The watcher starts at the current end of the file so historic
    alerts never re-notify, tolerates truncation or rotation by resetting its
    offset, and applies a cooldown so a repeated anomaly does not toast on
    every analyzer run.
    """

    def __init__(self, path: Path, cooldown_sec: float = 1800.0):
        self.path = Path(path)
        self.cooldown_sec = cooldown_sec
        try:
            self._offset = self.path.stat().st_size
        except OSError:
            self._offset = 0
        # Negative infinity so the very first alert always notifies, whatever
        # clock the caller passes in.
        self._last_notify = float("-inf")
        # weekly_digest lines (roadmap item 32) fire once per ISO week, so
        # dropping one to cooldown is not the same "seen again soon" fatigue
        # control an ordinary repeated anomaly gets — it is losing the only
        # delivery that line will ever get this week. A digest line seen
        # during cooldown is held here instead, and delivered on the next
        # poll where the cooldown has expired, even if that poll has no new
        # lines of its own.
        self._pending_digest: list[str] = []

    def poll(self, now: float | None = None) -> list[str]:
        """Return every unseen alert line, in order, when a toast is due, else [].

        A run can append more than one line at once (an event-driven anomaly
        alert and, on the first run of a new ISO week, the weekly digest) —
        both must be delivered, not just the last, so the caller notifies
        once per returned line. New ordinary lines that arrive inside the
        cooldown window are consumed silently (the offset still advances),
        which is what keeps one noisy anomaly from notifying on every run.
        A weekly_digest line arriving inside the cooldown is carried over
        (self._pending_digest) instead of being dropped the same way — see
        the comment in __init__ — and delivered once the cooldown allows.
        """
        if now is None:
            now = time.time()
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            self._offset = 0
        new_lines: list[str] = []
        if size != self._offset:
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    f.seek(self._offset)
                    new_text = f.read()
            except OSError:
                return []
            self._offset = size
            new_lines = [ln.strip() for ln in new_text.splitlines() if ln.strip()]

        if now - self._last_notify < self.cooldown_sec:
            if new_lines:
                self._pending_digest.extend(ln for ln in new_lines if "weekly_digest" in ln)
            return []

        deliver = self._pending_digest + new_lines
        self._pending_digest = []
        if not deliver:
            return []
        self._last_notify = now
        return deliver


# ----------------------------------------------------------------------
# Webhook delivery (item 23) — a second delivery path next to the toast,
# reusing AlertWatcher's tail/cooldown decision rather than duplicating it.
# ----------------------------------------------------------------------
def get_webhook_url() -> str | None:
    """Read the webhook URL from the OS keyring, or None if unset.

    A missing/unusable keyring backend is logged and treated the same as "no
    URL configured" rather than raised, so a machine without a keyring
    backend simply runs with the webhook channel silently inactive.
    """
    try:
        return keyring.get_password(KEYRING_SERVICE, WEBHOOK_KEYRING_USERNAME)
    except Exception as e:
        log(f"Webhook: keyring unavailable ({type(e).__name__}); no URL configured.")
        return None


def set_webhook_url(url: str) -> bool:
    """Store the webhook URL in the OS keyring. Mirrors the password's
    keyring-only storage: the URL never touches credentials.txt, config.toml,
    or the repo.

    A locked/unavailable keyring backend is logged and returns False rather
    than raising (mirroring _resolve_password's guarded keyring.set_password
    call above), so --set-webhook-url fails gracefully with a logged reason
    instead of a traceback. Returns True on success."""
    try:
        keyring.set_password(KEYRING_SERVICE, WEBHOOK_KEYRING_USERNAME, url)
        return True
    except Exception as e:
        log(f"Webhook: failed to store URL in the OS keyring ({type(e).__name__}: {e}).")
        return False


def clear_webhook_url() -> None:
    """Remove the webhook URL from the OS keyring. A harmless no-op if none
    is currently stored."""
    with contextlib.suppress(Exception):
        keyring.delete_password(KEYRING_SERVICE, WEBHOOK_KEYRING_USERNAME)


def send_webhook(url: str, text: str, timeout: float = 5.0) -> None:
    """POST `text` as a plain-text body to `url`.

    Fire-and-forget: a timeout, connection failure, non-2xx response, or any
    other exception is logged by failure class/status only — never the URL,
    which may embed a token or topic name — and swallowed rather than
    raised, so a bad or unreachable webhook endpoint can never disturb the
    caller (in practice, the tray's alert-toast path).
    """
    try:
        r = requests.post(
            url,
            data=text.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            timeout=timeout,
        )
        if r.ok:
            log("Webhook delivered.")
        else:
            log(f"Webhook delivery failed: HTTP {r.status_code}")
    except requests.exceptions.RequestException as e:
        log(f"Webhook delivery failed: {type(e).__name__}")
    except Exception as e:
        log(f"Webhook delivery failed (unexpected): {type(e).__name__}")


def maybe_send_webhook(webhook_enabled: bool, url: str | None, text: str,
                        timeout: float = 5.0) -> None:
    """Gate + deliver one webhook send.

    Sends only when `webhook_enabled` is true AND `url` is a configured
    (non-empty) keyring value; anything else — disabled, or enabled with no
    URL stored — is a logged no-op, never an error.
    """
    if not webhook_enabled:
        return
    if not url:
        log("Webhook: enabled but no URL is configured in the keyring; skipping.")
        return
    send_webhook(url, text, timeout=timeout)


def notify_alert(icon: Icon, alert: str, webhook_enabled: bool,
                  webhook_url: str | None) -> None:
    """Deliver one new alert line to every active channel: a tray toast
    (always) and, when configured, a webhook POST on its own daemon thread.

    The toast fires first and unconditionally — a slow or failing webhook
    (swallowed and logged inside maybe_send_webhook/send_webhook) can never
    delay or prevent it. The webhook_enabled/URL gate is checked here,
    before the thread spawn, so the fully-disabled (or enabled-but-
    unconfigured) path does no extra work at all — no thread, no daemon
    overhead — rather than spawning a thread whose only job is to
    immediately no-op. The logged no-op is unchanged; it now just runs
    synchronously on the polling thread instead of inside a spawned one.
    """
    icon.notify(alert, "UPS — alerta")
    log(f"Toast: {alert}")
    if not webhook_enabled:
        return
    if not webhook_url:
        log("Webhook: enabled but no URL is configured in the keyring; skipping.")
        return
    threading.Thread(
        target=maybe_send_webhook,
        args=(webhook_enabled, webhook_url, alert),
        daemon=True,
    ).start()


# ----------------------------------------------------------------------
# Status parsing
# ----------------------------------------------------------------------
@dataclass
class PcssStatus:
    battery_pct: int | None = None
    load_pct: float | None = None
    runtime_min: int | None = None
    input_v: float | None = None
    battery_v: float | None = None
    device_status: str | None = None


# PCSS renders each metric as:
#   <div class="value" id="value_<Field>"><number></div>
#   <div class="unit">%</div>
# Numbers use the active locale (Spanish: "54,0").
PCSS_VALUE_IDS = {
    "battery_pct":   "value_BatteryCharge",
    "load_pct":      "value_RealPowerPct",
    "runtime_min":   "value_RuntimeRemaining",
    "input_v":       "value_InputVoltage",
    "battery_v":     "value_VoltageDC",
    "device_status": "value_DeviceStatus",
}


def parse_status(html: str) -> PcssStatus:
    soup = BeautifulSoup(html, "html.parser")
    s = PcssStatus()

    for field_name, dom_id in PCSS_VALUE_IDS.items():
        el = soup.find(id=dom_id)
        if el is None:
            continue
        raw = el.get_text(strip=True)
        if field_name == "device_status":
            s.device_status = raw or None
            continue
        n = _parse_locale_number(raw)
        if n is None:
            continue
        if field_name == "battery_pct":
            s.battery_pct = _clamp_pct(int(round(n)))
        elif field_name == "load_pct":
            s.load_pct = max(0.0, min(100.0, n))
        elif field_name == "runtime_min":
            s.runtime_min = max(0, int(round(n)))
        else:
            setattr(s, field_name, n)

    # Fallback: label-based search if the canonical ids weren't found
    # (covers locale changes / future PCSS versions that rename ids)
    if s.battery_pct is None:
        s.battery_pct = _find_pct_near_label(
            soup,
            ("Carga de la batería", "Carga de la bateria",
             "Battery Charge", "Battery Capacity",
             "Capacidad de la batería"),
        )

    return s


def _parse_locale_number(text: str) -> float | None:
    """Parse Spanish-locale or English-locale numbers: '54,0' / '54.0' / '54'."""
    if not text:
        return None
    cleaned = text.strip().rstrip("%").strip()
    cleaned = cleaned.replace(".", "").replace(",", ".") \
        if cleaned.count(",") == 1 and cleaned.count(".") == 0 \
        else cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _find_pct_near_label(soup: BeautifulSoup, needles: tuple[str, ...]) -> int | None:
    for needle in needles:
        for tag in soup.find_all(string=re.compile(re.escape(needle), re.I)):
            for ancestor in _ancestors(tag, depth=4):
                text = ancestor.get_text(separator=" ")
                # Match "54", "54,0", "54.0" optionally followed by %
                m = re.search(r"(\d{1,3})(?:[.,]\d+)?\s*%?", text[len(needle):]
                              if needle in text else text)
                if m:
                    return _clamp_pct(int(m.group(1)))
    return None


def _ancestors(node, depth=3):
    cur = node
    for _ in range(depth):
        cur = getattr(cur, "parent", None)
        if cur is None:
            return
        yield cur


def _clamp_pct(n: int) -> int:
    return max(0, min(100, int(n)))


# ----------------------------------------------------------------------
# Icon rendering
# ----------------------------------------------------------------------
def color_for_pct(pct: int | None) -> tuple[int, int, int, int]:
    if pct is None:
        return (160, 160, 160, 255)   # gray (unknown)
    if pct >= 60:
        return (40, 200, 80, 255)     # green
    if pct >= 30:
        return (255, 180, 0, 255)     # amber
    return (220, 60, 60, 255)         # red


def _load_font(target_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("arialbd.ttf", "seguibl.ttf", "segoeuib.ttf",
                 "arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, target_size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_icon(pct: int | None, size: int = 64) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = color_for_pct(pct)

    # Geometry
    border = max(2, size // 24)
    body_l = border
    body_t = int(size * 0.20)
    body_r = int(size * 0.84)
    body_b = int(size * 0.80)
    cap_l = body_r
    cap_t = int(size * 0.36)
    cap_r = size - border
    cap_b = int(size * 0.64)

    # Cap (filled small nub on the right)
    draw.rectangle((cap_l, cap_t, cap_r, cap_b), fill=color)

    # Body outline
    draw.rectangle((body_l, body_t, body_r, body_b),
                   outline=color, width=border)

    # Charge fill — proportional rectangle inside the body
    if pct is not None and pct > 0:
        inset = border + 1
        inner_l = body_l + inset
        inner_t = body_t + inset
        inner_r = body_r - inset
        inner_b = body_b - inset
        fill_w = int((inner_r - inner_l) * max(1, min(100, pct)) / 100)
        if fill_w > 0:
            # Slightly translucent so the number on top stays readable
            r, g, b, _ = color
            draw.rectangle((inner_l, inner_t, inner_l + fill_w, inner_b),
                           fill=(r, g, b, 130))

    # Number — centered with anchor="mm" (Pillow 8+)
    text = "??" if pct is None else str(pct)
    body_h = body_b - body_t
    body_w = body_r - body_l
    # Pick font size that fits both height and width with margin
    target_h = int(body_h * 0.85)
    font = _load_font(target_h)
    # Shrink if width would overflow
    while True:
        bbox = draw.textbbox((0, 0), text, font=font, anchor="mm")
        tw = bbox[2] - bbox[0]
        if tw <= body_w - 2 * border or target_h <= 8:
            break
        target_h -= 2
        font = _load_font(target_h)

    cx = (body_l + body_r) / 2
    cy = (body_t + body_b) / 2

    # Outline (black) + fill (white) for contrast on any tray theme
    for dx, dy in ((-1, -1), (-1, 0), (-1, 1),
                   (0, -1),           (0, 1),
                   (1, -1),  (1, 0),  (1, 1)):
        draw.text((cx + dx, cy + dy), text, font=font,
                  fill=(0, 0, 0, 255), anchor="mm")
    draw.text((cx, cy), text, font=font,
              fill=(255, 255, 255, 255), anchor="mm")
    return img


# ----------------------------------------------------------------------
# Tray app
# ----------------------------------------------------------------------
@dataclass
class State:
    status: PcssStatus = field(default_factory=PcssStatus)
    last_update: float = 0.0
    last_error: str | None = None
    busy: bool = False  # True when PCSS rejected login because another session is connected


def _format_tooltip(state: State) -> str:
    s = state.status
    ts = (time.strftime("%H:%M:%S", time.localtime(state.last_update))
          if state.last_update else "--")
    lines = []
    if state.busy:
        lines.append("[en uso por navegador]")
    if s.battery_pct is not None:
        lines.append(f"Batería: {s.battery_pct}%")
    else:
        lines.append("Batería: ??")
    if s.runtime_min is not None:
        lines.append(f"Autonomía: {s.runtime_min} min")
    if s.load_pct is not None:
        lines.append(f"Carga SAI: {s.load_pct:.1f}%")
    if s.input_v is not None:
        lines.append(f"Entrada: {s.input_v:.0f} V")
    if s.battery_v is not None:
        lines.append(f"Batería: {s.battery_v:.1f} VCD")
    if s.device_status:
        lines.append(f"Estado: {s.device_status}")
    lines.append(f"({ts})")
    if state.last_error and not state.busy:
        lines.append(f"! {state.last_error}")
    # Windows tray tooltips are limited to 127 chars
    text = "\n".join(lines)
    return text[:127]


def _load_tray_config() -> Path | None:
    """Load config.toml anchored to SCRIPT_DIR, not the process's current
    working directory.

    The tray can be launched with any CWD (a pythonw shortcut, a Task
    Scheduler job at logon), so resolving config.toml relative to the
    process CWD (pcss_config.load_config()'s no-argument default) would
    silently leave WEBHOOK_ENABLED — and every other config key — at its
    default with no symptom. load_config() tolerates a nonexistent path
    gracefully, so this is safe whether or not config.toml exists next to
    tray_status.py.
    """
    return pcss_config.load_config(path=SCRIPT_DIR / "config.toml")


def main():
    # Single-instance check — refuse to run if another tray is active.
    # Concurrent instances corrupt PCSS form-tokens and cause login failures.
    instance_handle = acquire_single_instance()
    if instance_handle is None:
        msgbox(
            "PCSS tray ya está corriendo.\n\n"
            "Buscá el ícono en el system tray (esquina inferior derecha — "
            "puede estar oculto bajo la flecha ⌃).",
            "PCSS tray — ya activo",
            icon=0x30,  # MB_ICONWARNING
        )
        raise SystemExit(0)

    # Same config.toml the analyzer reads (module-level state in pcss.config);
    # this is how [alerts] webhook_enabled reaches the tray.
    _load_tray_config()

    cfg = load_credentials()
    client = PCSSClient(cfg["url"], cfg["username"], cfg["password"])
    state = State()
    watcher = AlertWatcher(OUTPUT / "alerts.log")
    analyzer_gate = SingleFlightRun()
    icon: Icon

    # Serializes all use of the shared requests.Session. The poll loop and the
    # "Refrescar ahora" menu item (which spawns a thread) both call refresh(),
    # and open_pcss() also touches client.session — concurrent access to one
    # requests.Session is not thread-safe.
    refresh_lock = threading.Lock()

    def refresh():
        with refresh_lock:
            try:
                status = client.get_status()
                state.status = status
                state.last_update = time.time()
                state.busy = False
                state.last_error = (None if status.battery_pct is not None
                                    else "Could not parse % from /status")
                log(f"battery={status.battery_pct}%  load={status.load_pct}%  "
                    f"runtime={status.runtime_min}min  inputV={status.input_v}  "
                    f"battV={status.battery_v}  state={status.device_status!r}")
            except UserAlreadyConnected as e:
                # Browser/another session is using PCSS — keep last-known values,
                # mark busy. Polling switches to fast (15s) interval so we recover
                # within seconds of the user closing the browser.
                state.busy = True
                state.last_update = time.time()
                log(f"PCSS busy: {e}")
            except Exception as e:
                state.busy = False
                state.last_error = f"{type(e).__name__}: {e}"
                state.status = PcssStatus()
                log(f"Refresh failed: {state.last_error}\n{traceback.format_exc()}")
        try:
            icon.icon = render_icon(state.status.battery_pct)
            icon.title = _format_tooltip(state)
            # Rebuild the popup menu so the dynamic status_label() refreshes.
            # On Win32 pystray only re-evaluates callables when update_menu()
            # is called; the right-click handler does NOT re-evaluate them.
            with contextlib.suppress(Exception):
                icon.update_menu()
        except Exception:
            pass
        # New analyzer alerts become a toast, and (when configured) a webhook
        # POST. A run can append more than one line (an anomaly alert and,
        # separately, the weekly digest), so every line polled is delivered,
        # not just the last. Fire-and-forget: notification failure must never
        # disturb the polling loop.
        try:
            for alert in watcher.poll():
                notify_alert(icon, alert, pcss_config.WEBHOOK_ENABLED, get_webhook_url())
        except Exception as e:
            log(f"Alert toast failed: {e}")

    def poll_loop():
        while True:
            refresh()
            # When busy (browser holds the PCSS user lock), poll more often so
            # we reclaim the session within seconds of the user closing it.
            wait = (_BUSY_POLL_INTERVAL_SEC if state.busy
                    else int(cfg["poll_interval_sec"]))
            time.sleep(wait)

    def open_pcss(_=None):
        # Share the script's session with the browser via URL session
        # rewriting (Java EE ;jsessionid=XXX), and launch in private/incognito
        # so the browser has no prior cookie to clash with the URL session.
        #
        # Why this works:
        #  - PCSS only allows one logged-in user at a time. By sharing the
        #    script's existing session, browser+script use ONE session — no
        #    "ya está conectado" rejection.
        #  - Incognito has empty cookies → Jetty doesn't see two valid
        #    sessions, so no "Duplicate sessions" 400.
        try:
            # Hold the same lock as refresh() so we never drive the shared
            # session from two threads at once.
            with refresh_lock:
                if not client._logged_in:
                    client.login()
                jsid = client.session.cookies.get("JSESSIONID")
            url = (f"{cfg['url']}/status;jsessionid={jsid}" if jsid
                   else f"{cfg['url']}/status")
        except Exception as e:
            log(f"open_pcss: session prep failed: {e}")
            url = f"{cfg['url']}/status"

        if not _open_private_window(url):
            log("No private-mode browser found; falling back to default.")
            webbrowser.open(url)

    def open_dashboard(_=None):
        dash = OUTPUT / "dashboard.html"
        if dash.exists():
            webbrowser.open(dash.as_uri())
        else:
            msgbox(f"No dashboard found.\nRun analyze_ups.py first to generate {dash}.")

    def force_refresh(_=None):
        threading.Thread(target=refresh, daemon=True).start()

    def update_dashboard(_=None):
        run_analyzer_now(icon, analyzer_gate)

    def quit_app(_=None):
        icon.stop()

    def status_label(_):
        pct = state.status.battery_pct
        if state.busy:
            return f"UPS: {pct}%  (en uso por navegador)" if pct is not None \
                else "UPS: en uso por navegador"
        if pct is not None:
            rt = state.status.runtime_min
            return f"UPS: {pct}%  ({rt} min)" if rt is not None else f"UPS: {pct}%"
        if state.last_error:
            return "UPS: error"
        return "UPS: ??"

    icon = Icon(
        "PCSS Battery",
        render_icon(None),
        title="PCSS battery — connecting...",
        menu=Menu(
            MenuItem(status_label, None, enabled=False),
            Menu.SEPARATOR,
            MenuItem("Abrir PCSS web UI", open_pcss, default=True),
            MenuItem("Abrir dashboard local", open_dashboard),
            MenuItem("Actualizar dashboard", update_dashboard),
            MenuItem("Refrescar ahora", force_refresh),
            Menu.SEPARATOR,
            MenuItem("Salir", quit_app),
        ),
    )

    threading.Thread(target=poll_loop, daemon=True).start()
    icon.run()


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------
def parse_cli_args(argv: list[str]) -> argparse.Namespace:
    """Parse tray_status.py's command-line flags.

    Normal operation takes no flags at all (this is what run_tray.bat and
    the tray shortcut invoke). --set-webhook-url and --clear-webhook-url are
    one-shot setup commands (roadmap item 23): each does its job and exits
    without starting the tray icon.
    """
    parser = argparse.ArgumentParser(description="PCSS battery tray icon.")
    parser.add_argument(
        "--set-webhook-url", action="store_true",
        help="Prompt for a webhook URL and store it in the OS keyring, then exit "
             "(mirrors the automatic password migration: the URL never touches "
             "credentials.txt or config.toml).",
    )
    parser.add_argument(
        "--clear-webhook-url", action="store_true",
        help="Remove the stored webhook URL from the OS keyring, then exit.",
    )
    return parser.parse_args(argv)


def _prompt_and_set_webhook_url() -> int:
    """Console setup command backing --set-webhook-url: prompts for the URL
    and stores it in the OS keyring. Not covered by the pytest suite (it
    calls input()); the storage it delegates to (set_webhook_url) is.

    Returns a process exit code: 0 on success, 1 if nothing was entered or
    the keyring store failed (set_webhook_url already logged the reason)."""
    url = input("Webhook URL (e.g. an ntfy.sh topic URL): ").strip()
    if not url:
        print("No URL entered; nothing stored.")
        return 1
    if not set_webhook_url(url):
        print(f"Failed to store the webhook URL in the OS keyring; see {TRAY_LOG}.")
        return 1
    print(f"Webhook URL stored in the OS keyring under service '{KEYRING_SERVICE}'.")
    print("Remember to also set [alerts] webhook_enabled = true in config.toml.")
    return 0


def _run_clear_webhook_url() -> None:
    """Console setup command backing --clear-webhook-url."""
    clear_webhook_url()
    print("Webhook URL removed from the OS keyring (if one was set).")


if __name__ == "__main__":
    cli_args = parse_cli_args(sys.argv[1:])
    if cli_args.set_webhook_url:
        sys.exit(_prompt_and_set_webhook_url())
    elif cli_args.clear_webhook_url:
        _run_clear_webhook_url()
    else:
        main()
