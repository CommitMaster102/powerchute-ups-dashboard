"""PCSS EventLog parsing.

The EventLog is a single Java ObjectOutputStream containing
``com.apcc.m11.arch.event.Event`` objects. Each event carries a
``java.util.Date`` timestamp, an active/cleared flag, and an ObjectId — a
path of integers such as ``3.5.1.5.4.1`` that identifies the event type.
There is no free text in the stream (no hostnames, no messages), which is
why a real capture can live in the test fixtures.

Decoding is generic: javaobj-py3 implements the documented Java
serialization grammar, so no APC class definitions and no hand-maintained
byte offsets are involved. Event names come from the ``.properties``
resource bundles inside the installed PCSS jars (they therefore always
match the installed PCSS version), with a small built-in fallback table for
the ids that matter when the jars are not reachable. Anything else about
the file — missing, unreadable, or a future format — degrades to a status
string; the analyzer treats the EventLog as optional and must keep doing so.
"""
from __future__ import annotations

import contextlib
import re
import struct
import zipfile
from pathlib import Path

import pandas as pd

from pcss import config

try:
    from javaobj.v2 import main as _javaobj
except ImportError:                       # pragma: no cover - exercised via monkeypatch
    _javaobj = None

# Event ids observed on the BX2000M-LM plus the ones a UPS owner cares
# about, named exactly as PCSS's own MicroLinkPowerSourceEvents.properties
# names them. Used when the installed jars are not available (for example
# when analyzing exported logs on another machine).
FALLBACK_NAMES = {
    "3.5.1.5.4.1": "On Battery",
    "3.5.1.5.4.2": "No Longer On Battery",
    "3.5.1.5.4.5": "Overload",
    "3.5.1.5.3.1": "Battery Needs Replacing",
    "3.5.1.5.3.3": "Low Battery",
    "3.5.1.5.3.4": "Battery Discharged",
    "3.5.1.5.3.15": "Low Runtime Remaining",
    "3.5.1.5.3.18": "Time On Battery Threshold Exceeded",
    "3.5.1.5.3.19": "Graceful Shutdown Started",
    "3.5.1.5.6.1": "Communication Established",
    "3.5.1.5.6.2": "Communication Lost",
    "3.5.1.5.6.9": "Monitoring Stopped",
    "3.5.1.5.6.10": "Monitoring Started",
}

# Event categories for the timeline panel. Each key is an
# ObjectId prefix; an event's category is the value of the longest prefix that
# aligns to the id's dot-separated segments, so a specific id such as the
# graceful-shutdown "3.5.1.5.3.19" can override the broader battery family it
# otherwise falls under. Communication and monitoring share the "3.5.1.5.6"
# stem but split apart because their leaf ids are listed individually. Any id
# matching no prefix is "other". Categories: power (mains outages and
# overload), battery (low battery, discharge, time-on-battery), shutdown,
# communication, monitoring, and other.
EVENT_CATEGORIES = {
    "3.5.1.5.4": "power",            # On Battery, No Longer On Battery, Overload
    "3.5.1.5.3": "battery",          # Low Battery, Discharged, Needs Replacing, ...
    "3.5.1.5.3.19": "shutdown",      # Graceful Shutdown Started
    "3.5.1.5.6.1": "communication",  # Communication Established
    "3.5.1.5.6.2": "communication",  # Communication Lost
    "3.5.1.5.6.9": "monitoring",     # Monitoring Stopped
    "3.5.1.5.6.10": "monitoring",    # Monitoring Started
}

# The categories shown by default on the event timeline. About 95 percent of
# recorded events are Monitoring and Communication churn from PC boots (plus
# unnamed "other" housekeeping ids), so those ship hidden and the legend
# toggles them on; the signal categories — power, battery, and shutdown —
# ship visible.
EVENT_DEFAULT_VISIBLE = ("power", "battery", "shutdown")


def categorize_event(oid: str) -> str:
    """Map an event ObjectId to its timeline category.

    Matching is by the longest EVENT_CATEGORIES prefix that aligns to the
    id's dot-separated segments — an exact match, or a prefix followed by a
    dot — so a string prefix that crosses a segment boundary (for example
    "3.5.1.5.6.1" against "3.5.1.5.6.10") never matches. An id matching no
    prefix returns "other".
    """
    best_cat = "other"
    best_len = -1
    for prefix, cat in EVENT_CATEGORIES.items():
        if (oid == prefix or oid.startswith(prefix + ".")) and len(prefix) > best_len:
            best_cat, best_len = cat, len(prefix)
    return best_cat


# The pair that brackets a mains outage.
ON_BATTERY_OID = "3.5.1.5.4.1"
OFF_BATTERY_OID = "3.5.1.5.4.2"

# The PCSS self-test event id is still unknown: the
# one-month capture behind this project's fixtures never happened to catch a
# self-test event. Detection falls back instead to the capacity-dip shape in
# pcss.stats.detect_self_tests. When a self-test is finally observed, it will
# show up in output/archive/events.csv as an unresolved numeric id ("event
# X.Y.Z...") at a timestamp that lines up with a Battery Charge dip; add that
# id here (and to FALLBACK_NAMES above, so it renders with a real name) once
# confirmed, and event-based detection then takes precedence over the shape
# heuristic.
SELF_TEST_EVENT_IDS: tuple[str, ...] = ()

_BUNDLE_LINE = re.compile(r"^\.((?:\d+\.)+\d+)\.(true|false|name)\s*=\s*(.+)$")


def load_event_names(agent_dir: Path | None = None) -> dict[tuple[str, bool], str]:
    """Read event names from the resource bundles inside the PCSS jars.

    Keys are ``(oid, active)``; a ``.name=`` entry (the FlexEvent style)
    covers both flag values. Returns an empty mapping when the agent
    directory or its jars are absent — the caller falls back to
    FALLBACK_NAMES.
    """
    agent_dir = Path(agent_dir or config.PCSS_AGENT)
    names: dict[tuple[str, bool], str] = {}
    comp = agent_dir / "comp"
    if not comp.is_dir():
        return names
    for jar in sorted(comp.glob("*.jar")):
        try:
            z = zipfile.ZipFile(jar)
        except Exception:
            continue
        with contextlib.closing(z):
            for entry in z.namelist():
                if not entry.endswith(".properties") or "vent" not in entry.rsplit("/", 1)[-1]:
                    continue
                try:
                    text = z.read(entry).decode("latin-1")
                except Exception:
                    continue
                for line in text.splitlines():
                    m = _BUNDLE_LINE.match(line.strip())
                    if not m:
                        continue
                    oid, kind, name = m.group(1), m.group(2), m.group(3).strip()
                    if kind == "name":
                        names.setdefault((oid, True), name)
                        names.setdefault((oid, False), name)
                    else:
                        names[(oid, kind == "true")] = name
    return names


def resolve_name(oid: str, active: bool, bundle_names: dict[tuple[str, bool], str]) -> str:
    """Best available name for one event: jar bundle, then fallback table,
    then a numeric label — an unknown id must still render, not vanish."""
    hit = bundle_names.get((oid, active))
    if hit:
        return hit
    base = FALLBACK_NAMES.get(oid)
    if base:
        return base if active else f"{base} (cleared)"
    return f"event {oid}" if active else f"event {oid} (cleared)"


def _field_map(inst) -> dict:
    """Flatten a javaobj v2 instance's per-class field maps into one dict."""
    out = {}
    for fields in inst.field_data.values():
        for fd, val in fields.items():
            out[fd.name] = val
    return out


def _java_date_ms(inst) -> int | None:
    """Epoch milliseconds out of a serialized java.util.Date.

    Date writes its long through writeObject block data, which javaobj
    surfaces as an annotation blob; the first eight bytes are the value.
    """
    if inst is None or not hasattr(inst, "annotations"):
        return None
    for anns in inst.annotations.values():
        for ann in anns:
            data = getattr(ann, "data", None)
            if data and len(data) >= 8:
                return int(struct.unpack(">q", data[:8])[0])
    return None


def load_eventlog(path: Path | None = None) -> tuple[pd.DataFrame, str]:
    """Parse the EventLog into (frame, status).

    The frame has columns ``ts`` (naive local wall-clock, like every other
    timestamp in the pipeline), ``ts_ms`` (true epoch milliseconds, timezone
    independent), ``oid``, ``active``, and ``name``. Status is ``"ok"``,
    ``"missing"``, ``"no-parser"`` (javaobj-py3 not installed), ``"empty"``,
    or ``"parse-error: ..."`` — the analyzer prints it and moves on.
    """
    path = Path(path or config.EVENTLOG)
    empty = pd.DataFrame(columns=["ts", "ts_ms", "oid", "active", "name"])
    if not path.exists():
        return empty, "missing"
    if _javaobj is None:
        return empty, "no-parser"
    try:
        data = path.read_bytes()
        if not data:
            return empty, "empty"
        contents = _javaobj.loads(data)
        bundle_names = load_event_names(path.parent)
        rows = []
        for obj in contents:
            f = getattr(obj, "field_data", None) and _field_map(obj)
            if not f or "theTimestamp" not in f:
                continue
            ms = _java_date_ms(f.get("theTimestamp"))
            if ms is None:
                continue
            oid_inst = f.get("theObjectId")
            ints = []
            if oid_inst is not None and hasattr(oid_inst, "field_data"):
                arr = _field_map(oid_inst).get("theObjectIds")
                for item in getattr(arr, "data", []) or []:
                    if item is not None and hasattr(item, "field_data"):
                        ints.append(_field_map(item).get("value"))
            oid = ".".join(str(i) for i in ints if i is not None)
            active = bool(f.get("theActiveFlag", True))
            rows.append({
                "ts_ms": ms,
                "oid": oid,
                "active": active,
                "name": resolve_name(oid, active, bundle_names),
            })
        if not rows:
            return empty, "empty"
        df = pd.DataFrame(rows).sort_values("ts_ms", kind="stable").reset_index(drop=True)
        # Local wall-clock, consistent with the DataLog/energylog timestamps
        # (java.util.Date stores true epoch ms; PCSS displays it local).
        df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True).dt.tz_convert(
            _local_tz()).dt.tz_localize(None)
        return df[["ts", "ts_ms", "oid", "active", "name"]], "ok"
    except Exception as e:
        return empty, f"parse-error: {type(e).__name__}: {e}"


def _local_tz():
    """The machine's zone, resolved once per call (cheap, and monkeypatchable)."""
    from datetime import datetime
    return datetime.now().astimezone().tzinfo


def on_battery_spans(events: pd.DataFrame) -> pd.DataFrame:
    """Pair On Battery events with their end into authoritative outage spans.

    An outage ends at the first following "No Longer On Battery" (its own
    id, or the On Battery event re-serialized with the active flag cleared).
    An outage still open at the end of the log gets ``open=True`` and a NaT
    end. Returns start, end, duration_min, open.
    """
    if events.empty:
        return pd.DataFrame(columns=["start", "end", "duration_min", "open"])
    spans = []
    start_ts = None
    start_ms = None
    for ts, ms, oid, active in events[["ts", "ts_ms", "oid", "active"]].itertuples(
            index=False, name=None):
        is_on = oid == ON_BATTERY_OID and active
        is_off = (oid == OFF_BATTERY_OID and active) or (oid == ON_BATTERY_OID and not active)
        if is_on and start_ts is None:
            start_ts, start_ms = ts, ms
        elif is_off and start_ts is not None:
            spans.append({"start": start_ts, "end": ts,
                          "duration_min": (ms - start_ms) / 60000.0, "open": False})
            start_ts = start_ms = None
    if start_ts is not None:
        spans.append({"start": start_ts, "end": pd.NaT,
                      "duration_min": float("nan"), "open": True})
    return pd.DataFrame(spans, columns=["start", "end", "duration_min", "open"])


# ----------------------------------------------------------------------
# Event archive: like the DataLog archive, parsed events outlive PCSS's own
# rotation. One CSV (events are tiny), deduplicated on the identity triple.
# ----------------------------------------------------------------------
_EVENT_KEY = ["ts_ms", "oid", "active"]


def _dedup_events(df: pd.DataFrame) -> pd.DataFrame:
    # keep="last" lets a later run with the PCSS jars reachable upgrade a
    # numeric fallback label to the real event name.
    return (df.drop_duplicates(subset=_EVENT_KEY, keep="last")
              .sort_values("ts_ms", kind="stable").reset_index(drop=True))


def append_event_archive(events: pd.DataFrame, archive_dir: Path | None = None) -> int:
    """Append parsed events to archive/events.csv. Idempotent; returns the
    number of newly stored rows."""
    archive_dir = Path(archive_dir or config.ARCHIVE_DIR)
    if events is None or events.empty:
        return 0
    archive_dir.mkdir(parents=True, exist_ok=True)
    f = archive_dir / "events.csv"
    existing = load_event_archive(archive_dir)
    merged = _dedup_events(pd.concat([existing, events], ignore_index=True)
                           if not existing.empty else events.copy())
    merged.to_csv(f, index=False)
    return len(merged) - len(existing)


def load_event_archive(archive_dir: Path | None = None) -> pd.DataFrame:
    archive_dir = Path(archive_dir or config.ARCHIVE_DIR)
    f = archive_dir / "events.csv"
    if not f.exists():
        return pd.DataFrame(columns=["ts", "ts_ms", "oid", "active", "name"])
    try:
        return pd.read_csv(f, parse_dates=["ts"])
    except Exception:
        return pd.DataFrame(columns=["ts", "ts_ms", "oid", "active", "name"])


def merge_event_frames(live: pd.DataFrame, archive: pd.DataFrame) -> pd.DataFrame:
    """Merge live and archived events; the overlap appears once."""
    frames = [f for f in (archive, live) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=["ts", "ts_ms", "oid", "active", "name"])
    if len(frames) == 1:
        return frames[0].reset_index(drop=True)
    return _dedup_events(pd.concat(frames, ignore_index=True))
