"""Spanish-locale pins for dashboard surfaces that previously had none.

The dashboard localizes its UI strings through ``pcss.dashboard._L`` and the
``_STRINGS_ES`` table (``[dashboard] language = "es"``). Numbers and dates
stay en-US in every language. These tests pin the Spanish wording for four
features whose localization was until now unverified: the staleness health
pill, the tariff rate tag, the battery-age subtitle, and the
bill-reconciliation table headers.
"""
from __future__ import annotations

import pandas as pd
import pytest

from pcss import config
from pcss.dashboard import _bills_table_html, _rate_tag_label, build_dashboard


@pytest.fixture
def spanish(monkeypatch):
    """Switch the dashboard language to Spanish for one test (the autouse
    config-restore fixture in conftest puts it back afterward)."""
    monkeypatch.setattr(config, "DASHBOARD_LANGUAGE", "es")


def _minimal_inputs():
    return dict(
        datalog_df=pd.DataFrame(), energy_df=pd.DataFrame(), hist=pd.DataFrame(),
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary={}, stats_table=pd.DataFrame(), gaps=pd.DataFrame(),
        voltage_anomalies=pd.DataFrame(), high_load_episodes=pd.DataFrame(), crossval={},
    )


# ---------------------------------------------------------------- staleness pill
def test_staleness_pill_wording_spanish(spanish):
    staleness = {"level": "crit", "age_hours": 50.0}
    html = build_dashboard(**_minimal_inputs(), staleness=staleness)
    assert "Fuente de datos desactualizada" in html      # the stale health-pill label
    assert "sin muestras nuevas en" in html              # the "no new samples in" clause


# ---------------------------------------------------------------- tariff rate tag
def test_rate_tag_label_current_rates_spanish(spanish):
    assert _rate_tag_label("current rates") == "tarifas actuales"


def test_rate_tag_label_dated_keeps_the_date_spanish(spanish):
    # The date suffix itself stays unlocalized, like every date on the page.
    assert _rate_tag_label("rates from 2026-01-01") == "tarifas desde 2026-01-01"


# ---------------------------------------------------------------- battery-age subtitle
def test_battery_age_subtitle_spanish(spanish):
    battery = {
        "status": "insufficient_history",
        "battery_age_days": 120.0,
        "battery_installed_on": pd.Timestamp("2026-03-01"),
    }
    html = build_dashboard(**_minimal_inputs(), battery=battery)
    assert "antigüedad de la batería" in html
    assert "120" in html                                 # the age figure stays a plain number
    assert "desde 2026-03-01" in html


# ---------------------------------------------------------------- bill reconciliation table
def test_bill_reconciliation_headers_spanish(spanish):
    reconciled = pd.DataFrame([{
        "period": "2026-06", "ups_kwh": 10.0, "billed_kwh": 100.0, "share_pct": 10.0,
        "ups_cost_tiered": 900.0, "billed_amount_crc": 9000.0,
        "implied_rate_crc_per_kwh": 90.0, "tariff_low": 80.0, "tariff_high": 100.0,
        "rate_tag": "current rates", "partial": False,
    }])
    html = _bills_table_html(reconciled)
    assert "Período" in html            # Period
    assert "Facturado kWh" in html      # Billed kWh
    assert "Proporción" in html         # Share
    assert "Notas" in html              # Notes


# ---------------------------------------------------------------- card-tool tooltips
def test_card_tool_tooltips_spanish(spanish):
    """The per-card tool button titles route through _L now, so a
    Spanish build reads "Exportar CSV" rather than the English source text."""
    html = build_dashboard(**_minimal_inputs())
    assert 'title="Exportar PNG"' in html
    assert 'title="Exportar CSV"' in html
    assert 'title="Expandir"' in html
    # The English source strings must not survive in a Spanish build.
    assert 'title="Export CSV"' not in html


def test_card_tool_tooltips_english_default():
    html = build_dashboard(**_minimal_inputs())
    assert 'title="Export PNG"' in html
    assert 'title="Export CSV"' in html


def test_bill_reconciliation_headers_english_unchanged():
    reconciled = pd.DataFrame([{
        "period": "2026-06", "ups_kwh": 10.0, "billed_kwh": 100.0, "share_pct": 10.0,
        "ups_cost_tiered": 900.0, "billed_amount_crc": 9000.0,
        "implied_rate_crc_per_kwh": 90.0, "tariff_low": 80.0, "tariff_high": 100.0,
        "rate_tag": "current rates", "partial": False,
    }])
    html = _bills_table_html(reconciled)
    assert "Billed kWh" in html
    assert "Share" in html
