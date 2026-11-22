"""Tägliche Prognose aus GHCR: Champion holen, Dokument, Ablage und Abruf für Pages."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest

from ml_train import forecast_ghcr as fg
from ml_train.config import load_config
from ml_train.testing import synthetic_snapshot
from wx_exchange.oci import EMPTY, MANIFEST
from wx_exchange.oci import Registry as Oci
from wx_exchange.testing import FakeRegistry

ROOT = Path(__file__).resolve().parents[3]
ISSUE = datetime(2023, 5, 11, 13, tzinfo=UTC)


def _oci(fake: FakeRegistry) -> Oci:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return Oci(fake.host, fake.repository, client, "bot", "secret", (0.0, 0.0))


@pytest.fixture
def cfg():  # type: ignore[no-untyped-def]
    c, _ = load_config(ROOT / "config/ml/de_lu_v1.yaml")
    c.stage1 = [t for t in c.stage1 if t.code != "gen_wind_offshore"]
    return c


def _put_version(oci: Oci, tag: str, man: dict[str, Any]) -> None:
    files = {
        "manifest.json": json.dumps(man).encode(),
        "model.json": b"{}",
        "stage1_load_actual_q50.txt": b"tree",
        "backtest.parquet": b"gross",
    }
    layers = []
    for name, data in files.items():
        layers.append(
            {
                "mediaType": fg.FILE_MEDIA,
                "digest": oci.push_blob(data),
                "size": len(data),
                "annotations": {fg.TITLE: name},
            }
        )
    oci.put_manifest(
        tag,
        {"schemaVersion": 2, "mediaType": MANIFEST, "config": EMPTY, "layers": layers},
    )


def test_pull_model_only_model_files(cfg, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-084, scope=function)"""
    from ml_train.models import TwoStageModel

    monkeypatch.setattr(
        TwoStageModel, "load", lambda c, d: SimpleNamespace(variant=SimpleNamespace(name="x"))
    )
    fake = FakeRegistry(repository="owner/predictiondb-models/de_lu")
    oci = _oci(fake)
    v = "de_lu_v1-kalender-202611200300-abc123"
    _put_version(oci, "champion", {"config": cfg.name, "version": v, "publishable": True})
    fake.requests.clear()
    ch = fg.pull_model(oci, cfg, "champion", tmp_path)
    assert ch.version == v and ch.tag == "champion"
    assert ch.manifest_sha256.startswith("sha256:")
    assert sorted(p.name for p in (tmp_path / v).iterdir()) == [
        "manifest.json",
        "model.json",
        "stage1_load_actual_q50.txt",
    ]
    # backtest.parquet wird nicht geladen: drei Blobs plus Manifest
    assert sum(1 for r in fake.requests if "/blobs/" in r.url.path and r.method == "GET") == 3
    _put_version(oci, "unfrei", {"config": cfg.name, "version": v, "publishable": False})
    with pytest.raises(ValueError, match="frei"):
        fg.pull_model(oci, cfg, "unfrei", tmp_path)
    _put_version(oci, "fremd", {"config": "de_lu_v2", "version": v, "publishable": True})
    with pytest.raises(ValueError, match="gehört zu"):
        fg.pull_model(oci, cfg, "fremd", tmp_path)
    with pytest.raises(KeyError):
        fg.pull_model(oci, cfg, "fehlt", tmp_path)


def test_wanted_runs_respects_delay() -> None:
    """@relation(SWR-MLT-084, scope=function)"""
    at = datetime(2026, 11, 21, 13, tzinfo=UTC)
    assert fg.wanted_runs(at, [0, 12], 6) == [
        datetime(2026, 11, 21, 0, tzinfo=UTC),
        datetime(2026, 11, 20, 12, tzinfo=UTC),
    ]
    late = datetime(2026, 11, 21, 19, tzinfo=UTC)
    assert fg.wanted_runs(late, [0, 12], 6)[0] == datetime(2026, 11, 21, 12, tzinfo=UTC)
    early = datetime(2026, 11, 21, 4, tzinfo=UTC)
    assert fg.wanted_runs(early, [0], 6) == [
        datetime(2026, 11, 20, tzinfo=UTC),
        datetime(2026, 11, 19, tzinfo=UTC),
    ]


def _champion(frame_lead_days: list[int]) -> fg.Champion:
    def predict(f: Any) -> dict[str, pd.DataFrame]:
        n = len(f.X)
        base = np.arange(n, dtype=float)
        q = pd.DataFrame({"q10": base, "q50": base + 1, "q90": base + 2})
        return {"load_actual": q * 1000, "da_price_de_lu": q, "gen_solar": q}

    model = SimpleNamespace(
        lead_days=frame_lead_days,
        variant=SimpleNamespace(name="kalender", requires_weather=False),
        features=lambda: [],
        predict=predict,
    )
    man = {"config": "de_lu_v1", "variant": {"name": "kalender"}, "train_issue_days": ["a", "b"]}
    return fg.Champion("de_lu_v1-kalender-1", "champion", "sha256:ab", man, model)  # type: ignore[arg-type]


def test_forecast_push_and_pull_recent(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-084, SWR-MLT-085, scope=function)"""
    snap = synthetic_snapshot("2022-06-01", "2023-06-01", weather_from=None)
    snap.manifest.update(
        {
            "sources": {"smard": "frei"},
            "publishable": True,
            "attribution": {"energy": "SMARD (CC BY 4.0)"},
            "ghcr": {"packages": {"energy": {"2023": "sha256:1"}}, "missing": []},
        }
    )
    ch = _champion(list(cfg.issue.lead_days))
    doc = fg.forecast(cfg, ch, snap, ISSUE)
    assert doc["schema"] == fg.SCHEMA
    assert doc["issue_time"] == "2023-05-11T13:00:00Z"
    # nur Last und Preis, je Stunde P10/P50/P90
    assert list(doc["targets"]) == ["da_price_de_lu", "load_actual"]
    row = doc["targets"]["load_actual"][0]
    assert set(row) == {"valid_time", "p10", "p50", "p90"}
    assert row["valid_time"] >= "2023-05-11T13:00:00Z"
    assert doc["model"]["version"] == ch.version and doc["model"]["tag"] == "champion"
    assert doc["inputs_missing"] == {}
    assert doc["attribution"].startswith("PredictionDB (the78mole); energy: SMARD")
    assert doc["units"]["da_price_de_lu"] == "EUR/MWh"

    files = fg.files_of(doc)
    assert set(files) == {"latest.json", "latest.csv", "2023-05-11.json"}
    csv = files["latest.csv"].decode().splitlines()
    assert csv[0] == "target,valid_time,p10,p50,p90"
    assert len(csv) == 1 + sum(len(v) for v in doc["targets"].values())

    fake = FakeRegistry(repository="owner/predictiondb-forecasts/de_lu")
    oci = _oci(fake)
    assert not fg.exists(oci, date(2023, 5, 11))
    assert fg.push(oci, doc) == ["2023-05-11", "latest"]
    older = {**doc, "issue_time": "2023-05-10T13:00:00Z"}
    fg.push(oci, older)
    fg.push(oci, doc)  # zuletzt wieder der jüngste Tag auf latest
    assert fg.exists(oci, date(2023, 5, 11))
    m = fake.manifest("2023-05-11")
    assert m["artifactType"] == fg.ARTIFACT
    assert m["annotations"]["io.predictiondb.model_version"] == ch.version

    out = tmp_path / "pages"
    assert fg.pull_recent(oci, out, days=1) == ["2023-05-11"]
    assert fg.pull_recent(oci, out, days=15) == ["2023-05-10", "2023-05-11"]
    index = json.loads((out / "index.json").read_text())
    assert [d["day"] for d in index["days"]] == ["2023-05-10", "2023-05-11"]
    latest = json.loads((out / "latest.json").read_text())
    assert latest["issue_time"] == "2023-05-11T13:00:00Z"
    assert (out / "latest.csv").read_text().startswith("target,")


def test_forecast_reports_missing_targets(cfg) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-085, scope=function)"""
    snap = synthetic_snapshot("2022-06-01", "2023-06-01", weather_from=None)
    snap.manifest.update(
        {"sources": {"smard": "frei"}, "ghcr": {"missing": ["energy:2023"]}, "attribution": {}}
    )
    doc = fg.to_document(
        pd.DataFrame(),
        _champion([1]),
        snap,
        ISSUE,
        weather_run=None,
        ensemble_run=None,
    )
    assert doc["targets"] == {}
    assert set(doc["inputs_missing"]) == {"da_price_de_lu", "load_actual", "ghcr:energy:2023"}


def test_day_tags() -> None:
    assert fg.day_tags(["latest", "2026-11-21", "2026-11-20", "2026-1-1", "x"]) == [
        "2026-11-20",
        "2026-11-21",
    ]
