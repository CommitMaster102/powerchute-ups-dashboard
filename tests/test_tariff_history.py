"""Unit tests for the Coopesantos tariff history CSV.

The coopesantos_history project's extract_rates.py writes historical_t_re.csv,
one row per archive.org or live capture of coopesantos.com/tarifas.
`pcss.config.load_config` reads it through `[paths] tariff_history_file`,
turns each published T-RE table into one TariffPeriod dated by its alcance
and trimestre labels, and merges those into TARIFF_HISTORY, where a
config.toml [[tariff.history]] entry with the same date wins. The fixture in
tests/fixtures/historical_t_re.csv is eight real rows copied from that file:
a duplicated 2019 capture, the "IIII" spelling of the fourth quarter, a late
2021 publication, an unsigned CVG difference, a 2023 capture without con-CVG
rates, the stale 2026-03-14 capture, and the live 2026 table.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pcss import config

FIXTURE = Path(__file__).parent / "fixtures" / "historical_t_re.csv"
HEADER = ("date,source,status,alcance,trimestre,first200_a,first200_b,first200_c,"
          "addl_a,addl_b,addl_c,url,snippet\n")


def _write_csv(tmp_path, rows):
    p = tmp_path / "historical_t_re.csv"
    p.write_text(HEADER + "".join(r + "\n" for r in rows), encoding="utf-8")
    return p


@pytest.mark.parametrize("alcance, trimestre, expected", [
    ("72 del 29-03-2019", "II", date(2019, 4, 1)),
    ("162 del 1-07-2020", "III", date(2020, 7, 1)),
    ("256 del 29-09-2020", "IIII", date(2020, 10, 1)),
    ("332 del 17-12-2020", "I", date(2021, 1, 1)),
    ("83 del 28-04-2021", "II", date(2021, 4, 28)),
    ("187 del 28-09-2023", "IV", date(2023, 10, 1)),
    ("161 del 16-12-2025", "I", date(2026, 1, 1)),
])
def test_effective_from_reproduces_the_real_labels(alcance, trimestre, expected):
    assert config.tariff_effective_from(alcance, trimestre) == expected


@pytest.mark.parametrize("alcance, trimestre", [
    ("sin alcance", "I"),
    ("161 del 16-12-2025", "V"),
    ("161 del 31-02-2025", "I"),
])
def test_effective_from_rejects_labels_it_cannot_read(alcance, trimestre):
    with pytest.raises(ValueError):
        config.tariff_effective_from(alcance, trimestre)


def test_fixture_loads_one_tariff_per_alcance():
    periods, summary, warnings = config._load_tariff_history_csv(FIXTURE)
    assert warnings == []
    assert [p.effective_from for p in periods] == [
        date(2019, 4, 1), date(2020, 10, 1), date(2021, 4, 28),
        date(2021, 7, 1), date(2025, 1, 1), date(2026, 1, 1)]
    latest = periods[-1]
    assert latest.source == "Alcance 161 del 16-12-2025"
    assert (latest.coopesantos_low, latest.coopesantos_high) == (75.50, 122.20)
    assert latest.tier_limit_kwh == 200.0
    assert summary == "6 tariffs from historical_t_re.csv (1 capture without con-CVG rates skipped)"


def test_rates_come_from_the_con_cvg_columns_not_the_unsigned_difference():
    """The 2021-09-23 capture prints the CVG difference as 2,64 although the
    con-CVG rate is below the structural one, so only column b is used."""
    periods, _summary, _warnings = config._load_tariff_history_csv(FIXTURE)
    q3_2021 = next(p for p in periods if p.effective_from == date(2021, 7, 1))
    assert (q3_2021.coopesantos_low, q3_2021.coopesantos_high) == (75.58, 122.31)


def test_pcss_flat_comes_from_the_tariff_keys(monkeypatch):
    monkeypatch.setattr(config, "PCSS_FLAT_RATE", 133.0)
    periods, _summary, _warnings = config._load_tariff_history_csv(FIXTURE)
    assert {p.pcss_flat for p in periods} == {133.0}


def test_missing_file_is_silent(tmp_path):
    assert config._load_tariff_history_csv(tmp_path / "absent.csv") == ([], "", [])


def test_missing_columns_are_reported_and_the_file_ignored(tmp_path):
    p = tmp_path / "historical_t_re.csv"
    p.write_text("date,status,alcance\n2026-10-21,OK,161 del 16-12-2025\n", encoding="utf-8")
    periods, summary, warnings = config._load_tariff_history_csv(p)
    assert periods == [] and summary == ""
    assert len(warnings) == 1
    assert "first200_b" in warnings[0] and "ignored" in warnings[0]


def test_disagreeing_captures_warn_and_the_latest_wins(tmp_path):
    p = _write_csv(tmp_path, [
        "2026-03-14,archive,OK,161 del 16-12-2025,I,78.17,75.40,,126.51,122.20,,u,s",
        "2026-10-21,live,OK,161 del 16-12-2025,I,78.17,75.50,,126.51,122.20,,u,s",
    ])
    periods, _summary, warnings = config._load_tariff_history_csv(p)
    assert len(periods) == 1
    assert periods[0].coopesantos_low == 75.50
    assert len(warnings) == 1
    assert "Alcance 161 del 16-12-2025" in warnings[0]


def test_unreadable_row_is_reported_and_skipped(tmp_path):
    p = _write_csv(tmp_path, [
        "2026-10-20,live,OK,161 del 16-12-2025,X,78.17,75.50,,126.51,122.20,,u,s",
        "2026-10-21,live,OK,161 del 16-12-2025,I,78.17,75.50,,126.51,122.20,,u,s",
    ])
    periods, _summary, warnings = config._load_tariff_history_csv(p)
    assert len(periods) == 1
    assert len(warnings) == 1
    assert "2026-10-20" in warnings[0] and "row skipped" in warnings[0]


def _config_with_csv(tmp_path, csv_path, extra=""):
    conf = tmp_path / "config.toml"
    conf.write_text(f"[paths]\ntariff_history_file = '{csv_path}'\n{extra}", encoding="utf-8")
    return conf


def test_load_config_merges_the_csv_and_a_config_entry_wins(tmp_path):
    conf = _config_with_csv(tmp_path, FIXTURE, (
        "\n[[tariff.history]]\n"
        "effective_from = 2026-01-01\n"
        "coopesantos_low = 70.0\n"
        "coopesantos_high = 120.0\n"
        "tier_limit_kwh = 200.0\n"
        "pcss_flat = 126.51\n"))
    config.load_config(conf)
    assert len(config.TARIFF_HISTORY) == 6
    entry_2026 = config.TARIFF_HISTORY[-1]
    assert entry_2026.effective_from == date(2026, 1, 1)
    assert entry_2026.coopesantos_low == 70.0
    assert entry_2026.source == ""
    assert config.tariff_source_for(date(2026, 8, 21)) == "rates from 2026-01-01"
    assert config.tariff_source_for(date(2025, 8, 21)) == "Alcance 204 del 17-12-2024"


def test_load_config_twice_does_not_add_the_csv_tariffs_twice(tmp_path):
    conf = _config_with_csv(tmp_path, FIXTURE)
    config.load_config(conf)
    config.load_config(conf)
    assert len(config.TARIFF_HISTORY) == 6
    assert config.TARIFF_HISTORY_SUMMARY.startswith("6 tariffs")


def test_tariff_rates_for_prices_a_period_with_the_csv_tariff(tmp_path):
    config.load_config(_config_with_csv(tmp_path, FIXTURE))
    low, high, tier_limit, _flat, tag = config.tariff_rates_for(date(2026, 8, 21))
    assert (low, high, tier_limit) == (75.50, 122.20, 200.0)
    assert tag == "rates from 2026-01-01"
    assert config.tariff_source_for(date(2026, 8, 21)) == "Alcance 161 del 16-12-2025"


def test_tariff_source_before_the_first_tariff_is_the_flat_fallback(tmp_path):
    config.load_config(_config_with_csv(tmp_path, FIXTURE))
    assert config.tariff_source_for(date(2018, 1, 1)) == "current rates"
