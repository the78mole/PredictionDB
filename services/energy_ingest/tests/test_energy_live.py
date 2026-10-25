"""Echte Abrufe bei den offenen Quellen. Läuft nur mit ``-m network``."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from energy_ingest.http import Http
from energy_ingest.sources.energy_charts import EnergyCharts
from energy_ingest.sources.smard import Smard
from energy_ingest.sources.steo import EiaSteo

DAY = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)


@pytest.mark.network
@pytest.mark.parametrize("dataset", ["da_price_de_lu", "load_actual", "gen_solar"])
def test_smard(dataset: str) -> None:
    with httpx.Client(timeout=60) as client:
        (data,) = Smard(Http(client)).fetch(dataset, DAY, DAY + timedelta(days=1))
    assert len(data.points) >= 24


@pytest.mark.network
@pytest.mark.parametrize("dataset", ["price", "public_power"])
def test_energy_charts(dataset: str) -> None:
    with httpx.Client(timeout=60) as client:
        out = EnergyCharts(Http(client)).fetch(dataset, DAY, DAY + timedelta(days=1))
    codes = {d.code for d in out}
    assert ("da_price_de_lu" in codes) if dataset == "price" else ("load_actual" in codes)
    assert all(len(d.points) >= 24 for d in out)


@pytest.mark.network
def test_eia_steo() -> None:
    """Die beiden letzten Ausgaben (die laufende fehlt bis zur Veröffentlichung)."""
    now = datetime.now(UTC)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        out = EiaSteo(Http(client)).fetch("outlook", now - timedelta(days=62), now)
    codes = {d.code for d in out}
    assert {"steo_brent_h00", "steo_henry_hub_h00", "steo_brent_h12"} <= codes
