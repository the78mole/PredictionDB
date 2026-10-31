"""Prognose-Stack ohne Netz: Paket, Datenabruf mit Rangfolge, Marktwerte, Wetter, OCI, HTTP."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
import yaml

from energy_ingest.model import HOUR, SeriesData, series
from ml_train.bundle import export_package
from ml_train.config import load_config
from ml_train.dataset import FeatureBuilder, issue_days_between
from ml_train.models import TwoStageModel
from ml_train.registry import Registry
from ml_train.testing import synthetic_snapshot
from pdb_predict.cli import next_slot
from pdb_predict.energy import EnergyCache, Window
from pdb_predict.package import OciClient, PackageSource, load_package, parse_ref
from pdb_predict.server import Api
from pdb_predict.service import Predictor, write_outputs
from pdb_predict.weather import technology_features
from wx_process.decode import decode_step
from wx_process.silver import assemble
from wx_process.testing import grid, make_step

ROOT = Path(__file__).resolve().parents[2]
ISSUE = datetime(2023, 9, 20, 8, tzinfo=UTC)


@pytest.fixture(scope="module")
def snap():  # type: ignore[no-untyped-def]
    return synthetic_snapshot("2022-06-01", "2023-10-01", weather_from=None)


@pytest.fixture(scope="module")
def package(snap, tmp_path_factory):  # type: ignore[no-untyped-def]
    cfg, _ = load_config(ROOT / "config/ml/de_lu_v1.yaml")
    cfg.stage1 = [t for t in cfg.stage1 if t.code != "gen_wind_offshore"]
    cfg.lgbm.n_estimators = 30
    tmp = tmp_path_factory.mktemp("pkg")
    cfg_path = tmp / "cfg.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True))
    cfg, _ = load_config(cfg_path)
    v = cfg.variant("kalender")
    fb = FeatureBuilder(snap, cfg)
    frame = fb.build(issue_days_between(date(2022, 7, 1), date(2023, 9, 1), 4))
    model = TwoStageModel(cfg, v).fit(frame)
    reg = Registry(str(tmp / "registry"), cfg)
    manifest = {"snapshot_id": "synthetic", "train_issue_days": ["2022-07-01", "2023-09-01"]}
    version = reg.register(model, manifest, None, "champion")
    out = tmp / "paket"
    export_package(reg, version, out, config_path=cfg_path, snapshot=snap, cache=tmp / "cache")
    return out


class FakeSource:
    """Liefert Reihen aus dem synthetischen Snapshot; ``broken`` wirft für diese Codes."""

    license = "Testlizenz"
    priority = 10
    free = True

    def __init__(self, code: str, energy: pd.DataFrame, broken: set[str] | None = None) -> None:
        self.code = code
        self.energy = energy
        self.broken = broken or set()
        self.calls: list[tuple[str, datetime, datetime]] = []

    def datasets(self) -> list[str]:
        return sorted(self.energy["code"].unique())

    def max_chunk(self, dataset: str) -> timedelta:
        return timedelta(days=91)

    def fetch(self, dataset: str, start: datetime, end: datetime) -> list[SeriesData]:
        self.calls.append((dataset, start, end))
        if dataset in self.broken:
            raise RuntimeError("Quelle gestört")
        code = {"price": "da_price_de_lu", "public_power": "load_actual"}.get(dataset, dataset)
        e = self.energy[self.energy["code"] == code]
        e = e[(e["ts"] >= pd.Timestamp(start)) & (e["ts"] < pd.Timestamp(end))]
        pts = [(t.to_pydatetime(), float(v)) for t, v in zip(e["ts"], e["value"], strict=True)]
        return [SeriesData(series(code), HOUR, pts)] if pts else []


def test_package_contents(package: Path) -> None:
    """@relation(SWR-MLT-070, scope=function)"""
    info = json.loads((package / "package.json").read_text())
    assert info["variant"] == "kalender"
    assert not info["requires_weather"]
    assert "backtest.parquet" not in info["files"]  # keine Ist-Preise im Paket
    assert set(info["codes"]) >= {"load_actual", "da_price_de_lu", "gen_solar"}
    assert all((package / f).exists() for f in info["files"])
    pkg = load_package(package)
    assert pkg.cfg.name == "de_lu_v1"
    assert "cap_pv" in pkg.model.features()


def test_predict_from_package(package: Path, snap, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    smard = FakeSource("smard", snap.energy)
    cache = EnergyCache(tmp_path, {"smard": smard})
    pkg = load_package(package)
    doc = Predictor(lambda: pkg, cache).run(ISSUE)
    price = doc["targets"]["da_price_de_lu"]
    assert len(price) == 7 * 24
    assert all(r["p10"] <= r["p50"] <= r["p90"] for r in price)
    assert doc["units"]["load_actual"] == "MW"
    assert doc["model"]["license"] == "CC-BY-4.0"
    assert any(s["code"] == "smard" for s in doc["sources"])
    # zweiter Lauf lädt nur noch das Fenster um heute
    n = len(smard.calls)
    Predictor(lambda: pkg, cache).run(ISSUE)
    assert len(smard.calls) <= n * 2
    out = tmp_path / "out"
    write_outputs(doc, out, 14)
    assert (out / "latest.csv").read_text().startswith("target,valid_time,p10,p50,p90")

    api = Api(out, stale_after_h=3)
    status, _, body = api.handle("/v1/forecast/da_price_de_lu")
    assert status == 200 and list(json.loads(body)["targets"]) == ["da_price_de_lu"]
    status, ctype, body = api.handle("/v1/forecast?target=load_actual&format=csv")
    assert ctype.startswith("text/csv") and body.decode().count("\n") == 1 + 7 * 24
    assert api.handle("/health")[0] == 200
    assert api.handle("/v1/forecast/unbekannt")[0] == 404


def test_fallback_to_next_source(snap, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    smard = FakeSource("smard", snap.energy, broken={"da_price_de_lu"})
    ec = FakeSource("energy_charts", snap.energy)
    cache = EnergyCache(tmp_path, {"smard": smard, "energy_charts": ec})
    need = {"da_price_de_lu": Window(ISSUE - timedelta(days=10), ISSUE)}
    errors = cache.update(need, now=ISSUE)
    assert errors == {}
    assert cache.read("da_price_de_lu")["source"].unique().tolist() == ["energy_charts"]


def test_published_keeps_earliest(tmp_path: Path) -> None:
    cache = EnergyCache(tmp_path, {})
    ts = pd.Timestamp("2026-10-01", tz="UTC")
    early = pd.DataFrame({"ts": [ts], "value": [1.0], "published": [ts], "source": "x"})
    late = early.assign(value=2.0, published=ts + pd.Timedelta(days=3))
    cache.merge("load_actual", early)
    cache.merge("load_actual", late)
    row = cache.read("load_actual").iloc[0]
    assert row["value"] == 2.0 and row["published"] == ts


def test_market_values_from_hourly(tmp_path: Path) -> None:
    hours = pd.date_range("2025-01-01", "2026-10-01", freq="h", tz="UTC", inclusive="left")
    rng = np.random.default_rng(1)
    energy = pd.concat(
        [
            pd.DataFrame({"ts": hours, "code": c, "value": rng.uniform(1, 100, len(hours))})
            for c in ("da_price_de_lu", "gen_solar", "gen_wind_onshore", "gen_wind_offshore")
        ]
    )
    cache = EnergyCache(tmp_path, {"smard": FakeSource("smard", energy)})
    now = datetime(2026, 10, 1, tzinfo=UTC)
    need = {
        "mw_base": Window(now - timedelta(days=200), now),
        "jw_solar": Window(now - timedelta(days=700), now),
    }
    assert cache.update(need, now=now) == {}
    mw = cache.read("mw_base").set_index("ts")["value"]
    sep = pd.Timestamp("2026-08-31 22:00", tz="UTC")  # 1. September Ortszeit
    p = energy[energy["code"] == "da_price_de_lu"].set_index("ts")["value"]
    assert mw[sep] == pytest.approx(p[(p.index >= sep) & (p.index < "2026-09-30 22:00")].mean())
    assert cache.read("jw_solar")["ts"].min() == pd.Timestamp("2024-12-31 23:00", tz="UTC")


def test_weather_features_per_technology() -> None:
    lat, lon = grid()
    steps = list(range(0, 7))
    decoded = {s: decode_step(make_step(s, lat, lon), s) for s in steps}
    run = datetime(2099, 1, 2, 6, tzinfo=UTC)
    ds = assemble("gfs", "0p25", run, decoded)
    clusters = pd.DataFrame(
        {
            "technology": ["wind_on", "wind_on", "wind_on", "pv_roof"],
            "cluster_no": [0, 0, 1, 0],
            "lat": [52.0, 53.0, 48.0, 50.0],
            "lon": [10.0, 10.0, 11.0, 8.0],
            "share": [0.5, 0.5, 1.0, 1.0],
            "capacity_mw": [100.0, 100.0, 600.0, 50.0],
            "hub_height_m": [120.0, 120.0, None, None],
        }
    )
    cc = pd.DataFrame(
        {
            "technology": ["wind_on", "wind_on", "pv_roof"],
            "cluster_no": [0, 1, 0],
            "day": [date(2099, 1, 1)] * 3,
            "capacity_mw": [200.0, 600.0, 50.0],
        }
    )
    wx = yaml.safe_load((ROOT / "config/process/gfs_0p25.yaml").read_text())
    df = technology_features(ds, clusters, cc, wx)
    w = df.pivot_table(index="valid_time", columns="feature", values="value")
    # t2m wird nach Clusterleistung gemittelt: (200 · T(52,5°) + 600 · T(48°)) / 800
    t_c0 = 280 + 0.01 * 3 + 0.1 * (52.5 - 50) - 273.15
    t_c1 = 280 + 0.01 * 3 + 0.1 * (48 - 50) - 273.15
    v3 = w.loc[pd.Timestamp(run) + pd.Timedelta(hours=3)]
    assert v3["wind_on_t2m"] == pytest.approx((200 * t_c0 + 600 * t_c1) / 800, abs=0.02)
    assert v3["wind_on_wind_power"] == pytest.approx(800 * v3["wind_on_wind_cf"], rel=0.3)
    assert "pv_roof_pv_power" in w.columns


def test_oci_pull_and_refresh(package: Path, tmp_path: Path) -> None:
    files = {f.name: f.read_bytes() for f in package.iterdir()}
    blobs = {"sha256:" + hashlib.sha256(b).hexdigest(): b for b in files.values()}
    layers: list[dict[str, Any]] = [
        {
            "digest": "sha256:" + hashlib.sha256(b).hexdigest(),
            "annotations": {"org.opencontainers.image.title": n},
        }
        for n, b in files.items()
    ]
    manifest: dict[str, Any] = {
        "schemaVersion": 2,
        "artifactType": "application/vnd.predictiondb.model.v1",
        "layers": layers,
    }
    seen: dict[str, int] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        seen[path] = seen.get(path, 0) + 1
        if path == "/token":
            return httpx.Response(200, json={"token": "abc"})
        if req.headers.get("Authorization") != "Bearer abc":
            return httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",'
                    'service="ghcr.io",scope="repository:x/m:pull"'
                },
            )
        if path.endswith("/manifests/champion"):
            return httpx.Response(200, json=manifest, headers={"Docker-Content-Digest": "sha256:1"})
        digest = path.rsplit("/", 1)[-1]
        return httpx.Response(200, content=blobs[digest])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert parse_ref("ghcr.io/x/m:champion") == ("ghcr.io", "x/m", "champion")
    assert parse_ref("/models") is None
    src = PackageSource("ghcr.io/x/m:champion", tmp_path, client, refresh_h=24)
    pkg = src.get()
    assert pkg.digest == "sha256:1" and pkg.info["variant"] == "kalender"
    assert src.get() is pkg  # innerhalb der Frist kein neuer Abruf
    assert seen["/v2/x/m/manifests/champion"] == 2  # 401, dann mit Token
    oci = OciClient(client)
    bad = {**manifest, "layers": [{**layers[0], "digest": "sha256:" + "0" * 64}]}
    blobs["sha256:" + "0" * 64] = b"falsch"
    with pytest.raises(ValueError, match="Prüfsumme"):
        oci.pull("ghcr.io", "x/m", bad, tmp_path / "kaputt")


def test_next_slot() -> None:
    now = datetime(2026, 10, 24, 18, 25, tzinfo=UTC)
    assert next_slot(now, 60, 20) == datetime(2026, 10, 24, 19, 20, tzinfo=UTC)
    assert next_slot(now.replace(minute=5), 60, 20) == datetime(2026, 10, 24, 18, 20, tzinfo=UTC)
    assert next_slot(now, 15, 5) == datetime(2026, 10, 24, 18, 35, tzinfo=UTC)
