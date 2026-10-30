"""Echter Abruf eines Schritts von AWS bzw. NOMADS. Läuft nur mit ``-m network``."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import pytest

from gribkit import iter_messages
from wx_ingest.bronze import Bronze
from wx_ingest.config import GfsConfig
from wx_ingest.ingest import GfsIngestor

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.network
@pytest.mark.parametrize("endpoint", ["aws", "nomads"])
async def test_one_step(bronze: Bronze, endpoint: str) -> None:
    """@relation(SWR-WXI-013, SWR-WXI-014, scope=function)"""
    import eccodes as ec

    cfg = GfsConfig.from_yaml(REPO / "config/sources/gfs_0p25.yaml")
    cfg.endpoints = [e for e in cfg.endpoints if e.name == endpoint]
    run = (datetime.now(UTC) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    async with httpx.AsyncClient(timeout=60) as client:
        result = await GfsIngestor(cfg, bronze, client).ingest_run(run, [6])
    assert result.steps[0].status == "stored", result.steps[0].errors

    m = await bronze.step_manifest(run, 6)
    assert m is not None
    assert not m.absent
    import obstore

    grib = bytes(
        await (await obstore.get_async(bronze.store, bronze.step_key(run, 6))).bytes_async()
    )
    msgs = list(iter_messages(grib))
    assert len(msgs) == m.n_messages >= 13
    h = ec.codes_new_from_message(next(x for x in msgs if _short(x) == "2t"))
    assert (ec.codes_get(h, "Ni"), ec.codes_get(h, "Nj")) == (81, 57)
    assert ec.codes_get(h, "latitudeOfFirstGridPointInDegrees") == 58
    v = ec.codes_get_values(h)
    assert np.min(v) > 220 and np.max(v) < 330
    ec.codes_release(h)


def _short(msg: bytes) -> str:
    import eccodes as ec

    h = ec.codes_new_from_message(msg)
    try:
        return str(ec.codes_get(h, "shortName"))
    finally:
        ec.codes_release(h)
