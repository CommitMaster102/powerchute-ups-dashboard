"""Unit tests for battery lifecycle annotations.

A user-owned annotations.csv records dated lifecycle entries — battery
replaced, a new appliance added, the UPS moved — so the archive's history
stays interpretable years later. `pcss.loaders.load_annotations` reads it
defensively, exactly like `load_bills`: a missing file
silently disables the feature, and a malformed row is reported and skipped
rather than raising. `pcss.stats.latest_battery_replacement` picks the
fit-segmentation boundary `battery_replace_projection` uses: the newest
`battery_replaced` entry that is not dated later than the data being
analyzed (a future-dated entry marks no boundary yet).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from pcss import config
from pcss.loaders import load_annotations
from pcss.stats import latest_battery_replacement


def _write_annotations(tmp_path, text):
    p = tmp_path / "annotations.csv"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- loader
def test_default_annotations_file_is_repo_root_annotations_csv():
    assert config.ANNOTATIONS_FILE.name == "annotations.csv"


def test_missing_file_returns_empty_with_no_warnings(tmp_path):
    df, warnings = load_annotations(tmp_path / "does-not-exist.csv")
    assert df.empty
    assert list(df.columns) == ["date", "kind", "label"]
    assert warnings == []


def test_loads_a_valid_file(tmp_path):
    p = _write_annotations(
        tmp_path,
        "date,kind,label\n"
        "2026-01-15,battery_replaced,New battery installed\n"
        "2026-03-01,appliance_added,Added a space heater\n",
    )
    df, warnings = load_annotations(p)
    assert warnings == []
    assert list(df["date"]) == [date(2026, 1, 15), date(2026, 3, 1)]
    assert list(df["kind"]) == ["battery_replaced", "appliance_added"]
    assert list(df["label"]) == ["New battery installed", "Added a space heater"]


def test_malformed_date_is_skipped_and_reported(tmp_path):
    p = _write_annotations(
        tmp_path,
        "date,kind,label\n"
        "not-a-date,battery_replaced,oops\n"
        "2026-03-01,ups_moved,Moved to the office\n",
    )
    df, warnings = load_annotations(p)
    assert len(df) == 1
    assert df["date"].iloc[0] == date(2026, 3, 1)
    assert len(warnings) == 1
    assert "not-a-date" in warnings[0]


def test_missing_kind_is_skipped_and_reported(tmp_path):
    p = _write_annotations(
        tmp_path,
        "date,kind,label\n"
        "2026-01-15,,New battery installed\n"
        "2026-03-01,ups_moved,Moved to the office\n",
    )
    df, warnings = load_annotations(p)
    assert len(df) == 1
    assert df["kind"].iloc[0] == "ups_moved"
    assert len(warnings) == 1
    assert "2026-01-15" in warnings[0]


def test_missing_required_column_reports_and_ignores_the_file(tmp_path):
    p = _write_annotations(tmp_path, "date,kind\n2026-01-15,battery_replaced\n")
    df, warnings = load_annotations(p)
    assert df.empty
    assert len(warnings) == 1
    assert "label" in warnings[0]


def test_blank_label_is_kept_as_empty_string(tmp_path):
    p = _write_annotations(tmp_path, "date,kind,label\n2026-01-15,battery_replaced,\n")
    df, warnings = load_annotations(p)
    assert warnings == []
    assert df["label"].iloc[0] == ""


def test_freeform_kind_is_not_validated(tmp_path):
    """Only battery_replaced is recognized for fit segmentation, but any
    other kind loads cleanly — it still rides the payload as a marker."""
    p = _write_annotations(tmp_path, "date,kind,label\n2026-01-15,something_unusual,note\n")
    df, warnings = load_annotations(p)
    assert warnings == []
    assert df["kind"].iloc[0] == "something_unusual"


# ---------------------------------------------------------------- latest_battery_replacement
def test_none_or_empty_annotations_returns_none():
    assert latest_battery_replacement(None, pd.Timestamp("2026-05-01")) is None
    empty = pd.DataFrame(columns=["date", "kind", "label"])
    assert latest_battery_replacement(empty, pd.Timestamp("2026-05-01")) is None


def test_ignores_non_battery_replaced_kinds():
    df = pd.DataFrame({
        "date": [date(2026, 1, 1)], "kind": ["ups_moved"], "label": ["moved"],
    })
    assert latest_battery_replacement(df, pd.Timestamp("2026-06-01")) is None


def test_returns_newest_at_or_before_as_of():
    df = pd.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 3, 1)],
        "kind": ["battery_replaced", "battery_replaced"],
        "label": ["first", "second"],
    })
    result = latest_battery_replacement(df, pd.Timestamp("2026-06-01"))
    assert result == pd.Timestamp("2026-03-01")


def test_future_dated_entry_is_ignored():
    """An entry dated after the data being analyzed marks no boundary yet —
    it must not truncate the fit down to nothing."""
    df = pd.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 12, 1)],
        "kind": ["battery_replaced", "battery_replaced"],
        "label": ["first", "not yet"],
    })
    result = latest_battery_replacement(df, pd.Timestamp("2026-06-01"))
    assert result == pd.Timestamp("2026-01-01")


def test_boundary_exactly_at_as_of_is_included():
    df = pd.DataFrame({
        "date": [date(2026, 6, 1)], "kind": ["battery_replaced"], "label": ["x"],
    })
    result = latest_battery_replacement(df, pd.Timestamp("2026-06-01"))
    assert result == pd.Timestamp("2026-06-01")


def test_mixed_kinds_only_battery_replaced_counts():
    df = pd.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 4, 1), date(2026, 5, 1)],
        "kind": ["battery_replaced", "ups_moved", "appliance_added"],
        "label": ["a", "b", "c"],
    })
    result = latest_battery_replacement(df, pd.Timestamp("2026-06-01"))
    assert result == pd.Timestamp("2026-01-01")


# ---------------------------------------------------------------- end-to-end (analyze_ups.main)
def _write_battery_agent(agent, days, start=datetime(2026, 1, 1),
                         slope_per_day=-0.01, start_v=27.4):
    """A DataLog with a steadily declining Battery Voltage column, at the
    default 20-minute cadence, no energylog (the battery projection needs
    only the DataLog)."""
    agent.mkdir(parents=True, exist_ok=True)
    n = days * 72
    dl = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(n):
        t = start + timedelta(minutes=20 * i)
        bv = start_v + slope_per_day * (i * 20 / (24 * 60))
        vals = f"120.0\t{bv:.2f}\t15.0\t100".replace(".", ",")
        dl.append(f"{t:%m/%d/%Y %H:%M:%S}\t{vals}")
    (agent / "DataLog").write_text("\n".join(dl) + "\n", encoding="utf-8")
    return agent


def _hermetic_config(tmp_path, annotations_file=None):
    """A config that keeps the pipeline run hermetic (no real archive) and
    optionally overrides [paths] annotations_file."""
    lines = ["[archive]", "enabled = false"]
    if annotations_file is not None:
        lines += ["", "[paths]", f"annotations_file = '{annotations_file}'"]
    conf = tmp_path / "config.toml"
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return conf


def test_console_shows_battery_age_when_boundary_exists(tmp_path, capsys):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    ann_path = tmp_path / "annotations.csv"
    boundary = date(2026, 1, 3)
    ann_path.write_text(
        "date,kind,label\n"
        f"{boundary.isoformat()},battery_replaced,New battery installed\n",
        encoding="utf-8",
    )
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(ann_path)))])
    out = capsys.readouterr().out
    assert "BATTERY REPLACE-BY PROJECTION" in out
    assert "Battery installed" in out
    assert str(boundary) in out


def test_console_omits_battery_installed_when_file_missing(tmp_path, capsys):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    missing = tmp_path / "no-such-annotations.csv"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(missing)))])
    out = capsys.readouterr().out
    assert "Battery installed" not in out


def test_console_warns_on_malformed_annotation_row(tmp_path, capsys):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    ann_path = tmp_path / "annotations.csv"
    ann_path.write_text("date,kind,label\nnot-a-date,battery_replaced,oops\n", encoding="utf-8")
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(ann_path)))])
    out = capsys.readouterr().out
    assert "not-a-date" in out


def test_json_summary_includes_battery_installed_on(tmp_path):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    ann_path = tmp_path / "annotations.csv"
    boundary = date(2026, 1, 3)
    ann_path.write_text(
        "date,kind,label\n"
        f"{boundary.isoformat()},battery_replaced,New battery installed\n",
        encoding="utf-8",
    )
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(ann_path))),
                      "--json", str(j)])
    data = json.loads(j.read_text())
    assert data["battery"]["battery_installed_on"] == str(boundary)


def test_json_summary_omits_battery_installed_on_when_file_missing(tmp_path):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    missing = tmp_path / "no-such-annotations.csv"
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(missing))),
                      "--json", str(j)])
    data = json.loads(j.read_text())
    assert data["battery"]["battery_installed_on"] is None


def test_dashboard_shows_battery_age_in_subtitle(tmp_path):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    ann_path = tmp_path / "annotations.csv"
    boundary = date(2026, 1, 3)
    ann_path.write_text(
        "date,kind,label\n"
        f"{boundary.isoformat()},battery_replaced,New battery installed\n",
        encoding="utf-8",
    )
    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(ann_path)))])
    html = out.read_text(encoding="utf-8")
    assert "battery age" in html


def test_dashboard_payload_carries_the_annotation_marker(tmp_path):
    import analyze_ups
    agent = _write_battery_agent(tmp_path / "agent", days=10)
    ann_path = tmp_path / "annotations.csv"
    ann_path.write_text(
        "date,kind,label\n2026-01-03,battery_replaced,New battery installed\n",
        encoding="utf-8",
    )
    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, annotations_file=str(ann_path)))])
    html = out.read_text(encoding="utf-8")
    import re
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(payload["annotations"]) == 1
    assert payload["annotations"][0]["label"] == "New battery installed"
