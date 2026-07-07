"""
PCSS battery tray icon.

Polls https://localhost:6547/status periodically, logs in automatically using
the credentials in credentials.txt, and shows the UPS battery percentage as a
dynamic system-tray icon (battery silhouette + number, color-coded).

Right-click menu:
  - Current % (info, disabled)
  - Open PCSS web UI
  - Open local dashboard (analyze_ups.py output)
  - Refresh now
  - Exit

To run silently (no console), use run_tray.bat (which calls pythonw.exe).
For autostart, drop a shortcut to run_tray.bat into:
  shell:startup     (Win+R, type that, paste the shortcut)
"""
from __future__ import annotations

import contextlib
import ctypes
import re
import ssl
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

    def poll(self, now: float | None = None) -> str | None:
        """Return the newest unseen alert line when a toast is due, else None.

        New lines that arrive inside the cooldown window are consumed
        silently (the offset still advances), which is what keeps one noisy
        anomaly from notifying on every run.
        """
        if now is None:
            now = time.time()
        try:
            size = self.path.stat().st_size
        except OSError:
            return None
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return None
        try:
            with self.path.open("r", encoding="utf-8") as f:
                f.seek(self._offset)
                new_text = f.read()
        except OSError:
            return None
        self._offset = size
        lines = [ln.strip() for ln in new_text.splitlines() if ln.strip()]
        if not lines:
            return None
        if now - self._last_notify < self.cooldown_sec:
            return None
        self._last_notify = now
        return lines[-1]


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

    cfg = load_credentials()
    client = PCSSClient(cfg["url"], cfg["username"], cfg["password"])
    state = State()
    watcher = AlertWatcher(OUTPUT / "alerts.log")
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
        # New analyzer alerts become a toast. Fire-and-forget: notification
        # failure must never disturb the polling loop.
        try:
            alert = watcher.poll()
            if alert:
                icon.notify(alert, "UPS — alerta")
                log(f"Toast: {alert}")
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
            MenuItem("Refrescar ahora", force_refresh),
            Menu.SEPARATOR,
            MenuItem("Salir", quit_app),
        ),
    )

    threading.Thread(target=poll_loop, daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
