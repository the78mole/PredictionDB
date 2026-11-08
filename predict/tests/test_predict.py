"""Prognose-Stack ohne Netz: Paket, Datenabruf mit Rangfolge, Marktwerte, Wetter, OCI, HTTP."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
import yaml

from energy_ingest.model import HOUR, SeriesData, series
from ml_train.bundle import export_package, neighbour_cells, write_package
from ml_train.config import NeighbourWeather, Variant, load_config
from ml_train.dataset import FeatureBuilder, issue_days_between
from ml_train.models import TwoStageModel
from ml_train.registry import Registry
from ml_train.snapshot import Snapshot
from ml_train.testing import synthetic_snapshot
from pdb_predict.cli import next_slot
from pdb_predict.energy import EnergyCache, Window
from pdb_predict.package import OciClient, PackageSource, load_package, parse_ref
from pdb_predict.server import Api
from pdb_predict.service import Predictor, write_outputs
from pdb_predict.weather import WeatherCache, neighbour_features, technology_features
from wx_ingest.config import GfsConfig
from wx_process.decode import decode_step
from wx_process.features import FeatureBuilder as GoldBuilder
from wx_process.features import PvModel, TechnologyFeatures
from wx_process.regions import RegionSet
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
    cfg.lgbm.n_jobs = 1  # ein Thread je Testprozess (pytest-xdist)
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
    train = True

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
    """@relation(SWR-MLT-070, SWR-PRD-010, scope=function)"""
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
    """@relation(SWR-PRD-024, SWR-PRD-030, SWR-PRD-031, SWR-PRD-032, scope=function)"""
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
    """@relation(SWR-PRD-020, scope=function)"""
    smard = FakeSource("smard", snap.energy, broken={"da_price_de_lu"})
    ec = FakeSource("energy_charts", snap.energy)
    cache = EnergyCache(tmp_path, {"smard": smard, "energy_charts": ec})
    need = {"da_price_de_lu": Window(ISSUE - timedelta(days=10), ISSUE)}
    errors = cache.update(need, now=ISSUE)
    assert errors == {}
    assert cache.read("da_price_de_lu")["source"].unique().tolist() == ["energy_charts"]


def test_published_keeps_earliest(tmp_path: Path) -> None:
    """@relation(SWR-PRD-021, scope=function)"""
    cache = EnergyCache(tmp_path, {})
    ts = pd.Timestamp("2026-10-01", tz="UTC")
    early = pd.DataFrame({"ts": [ts], "value": [1.0], "published": [ts], "source": "x"})
    late = early.assign(value=2.0, published=ts + pd.Timedelta(days=3))
    cache.merge("load_actual", early)
    cache.merge("load_actual", late)
    row = cache.read("load_actual").iloc[0]
    assert row["value"] == 2.0 and row["published"] == ts


def test_market_values_from_hourly(tmp_path: Path) -> None:
    """@relation(SWR-PRD-022, scope=function)"""
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
    """@relation(SWR-PRD-023, scope=function)"""
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


#: Nachrichten in ``wx_process.testing`` zu ``pdb_predict.weather.NEIGHBOUR_MESSAGES``
NEIGHBOUR_NAMES = {"t2m", "u10", "v10", "u100", "v100", "dswrf"}
NB = NeighbourWeather(zones=["FR", "DK1"])
REGIONS = ROOT / "config/gefs/regions_v2.json.gz"


def eu_steps(names: set[str] | None = None) -> dict[int, bytes]:
    lat, lon = grid(71.5, 34.5, -10.5, 45, 0.5)
    return {s: make_step(s, lat, lon, names=names) for s in (0, 3, 6)}


def eu_dataset(blobs: dict[int, bytes], run: datetime):  # type: ignore[no-untyped-def]
    decoded = {s: decode_step(b, s) for s, b in blobs.items()}
    return assemble("gfs", "0p5", run, decoded).quantized()


def test_neighbour_features_like_gold() -> None:
    """Die Teilmenge der Nachrichten liefert dieselben Werte wie das Gold aus allen Nachrichten.

    @relation(SWR-PRD-025, SWR-MLT-071, scope=function)
    """
    run = datetime(2099, 1, 2, 6, tzinfo=UTC)
    wx = yaml.safe_load((ROOT / "config/process/gfs_0p5.yaml").read_text())
    regions = RegionSet.load(REGIONS)
    ids = {c: i for i, c in enumerate(regions.codes)}
    techs = {k: TechnologyFeatures.model_validate(v) for k, v in wx["technologies"].items()}
    builder = GoldBuilder(techs, PvModel.model_validate(wx["pv"]))
    series, _ = builder.build(eu_dataset(eu_steps(), run), regions.clusters(ids))
    gold = {(s.region_id, s.code): s.values for s in series}

    cells = neighbour_cells(NB, REGIONS)
    assert set(cells["technology"]) == {"area"}
    df = neighbour_features(eu_dataset(eu_steps(NEIGHBOUR_NAMES), run), cells, wx, NB.features)
    assert set(df["feature"]) == {
        "nb_fr_wind_cf",
        "nb_fr_pv_cf",
        "nb_dk1_wind_cf",
        "nb_dk1_pv_cf",
    }
    assert set(df.columns) == {"run_time", "valid_time", "feature", "value"}
    w = df.pivot(index="valid_time", columns="feature", values="value")
    for zone in ("FR", "DK1"):
        rid = ids[f"gefs:{zone}:area"]
        for code in ("wind_cf", "pv_cf"):
            got = w[f"nb_{zone.lower()}_{code}"]
            ref = gold[(rid, code)]
            np.testing.assert_allclose(got.dropna(), ref[~np.isnan(ref)], rtol=1e-5, atol=1e-6)
    # Strahlung fehlt im Analyse-Schritt, das Gold hat dort keine Zeile
    assert (df["feature"] == "nb_fr_pv_cf").sum() == 2
    assert (df["feature"] == "nb_fr_wind_cf").sum() == 3


def test_neighbour_cache_and_failure(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-PRD-025, scope=function)"""
    wx = yaml.safe_load((ROOT / "config/process/gfs_0p5.yaml").read_text())
    cells = neighbour_cells(NB, REGIONS)

    pkg = SimpleNamespace(
        version="v",
        neighbour_cells=cells,
        neighbour_wx={"technologies": wx["technologies"], "pv": wx["pv"]},
        cfg=SimpleNamespace(neighbour_weather=NB),
    )

    eu = GfsConfig.from_yaml(ROOT / "config/sources/gfs_0p5.yaml")
    de = GfsConfig.from_yaml(ROOT / "config/sources/gfs_0p25.yaml")
    cache = WeatherCache(tmp_path, de, run_hours=[0, 12], max_lead_h=6, neighbour_gfs=eu)
    run = datetime(2099, 1, 2, 12, tzinfo=UTC)
    calls: list[tuple[str, list[int]]] = []
    blobs: dict[int, bytes] | None = eu_steps(NEIGHBOUR_NAMES)

    async def download(gfs, run, steps, messages):  # type: ignore[no-untyped-def]
        calls.append((gfs.code, steps))
        return blobs

    monkeypatch.setattr(cache, "_download", download)
    monkeypatch.setattr(cache, "wanted_runs", lambda now: [run])
    monkeypatch.setattr(cache, "features_for", lambda run, package: None)
    df = cache.update(pkg)  # type: ignore[arg-type]
    assert calls == [("gfs_0p5", [0, 3, 6])]
    assert df["feature"].str.startswith("nb_").all() and not df.empty
    cache.update(pkg)  # type: ignore[arg-type]
    assert len(calls) == 1  # zweiter Aufruf aus dem Cache

    # Lauf der Quelle unvollständig oder Quelle falsch: keine Merkmale, kein Abbruch
    (tmp_path / "weather/nb/2099010212.parquet").unlink()
    blobs = None
    assert cache.update(pkg).empty  # type: ignore[arg-type]
    cache.neighbour_gfs = de
    assert cache.update(pkg).empty  # type: ignore[arg-type]


def test_neighbour_package_and_predict(tmp_path: Path) -> None:
    """Paket mit ``nachbarwetter``: Abbruch ohne Zellen, Prognose mit und ohne die Merkmale.

    @relation(SWR-MLT-071, SWR-PRD-025, SWR-PRD-024, scope=function)
    """
    snap = synthetic_snapshot("2022-06-01", "2023-10-01", weather_from="2023-01-01")
    w = snap.weather
    src = w[w["feature"] == w["feature"].iloc[0]]
    src = src[(src["valid_time"] - src["run_time"]) / pd.Timedelta(hours=1) % 3 == 0]
    nb_rows = pd.concat(
        [
            src.assign(feature=f"nb_{z}_wind_cf", value=src["value"].astype("float32"))
            for z in ("fr", "dk1")
        ]
    )
    train = Snapshot(snap.energy, pd.concat([w, nb_rows]), snap.capacity, snap.manifest)
    cfg_path = tmp_path / "cfg.yaml"
    cfg, _ = load_config(ROOT / "config/ml/de_lu_v1.yaml")
    cfg.stage1 = [t for t in cfg.stage1 if t.code != "gen_wind_offshore"]
    cfg.lgbm.n_estimators = 5
    cfg.lgbm.n_jobs = 1
    cfg.neighbour_weather = NB
    groups = ["calendar", "lags", "capacity", "weather", "nachbarwetter", "stage1"]
    cfg.variants = [Variant(name="nb", groups=groups, requires_weather=True)]
    cfg_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json"), allow_unicode=True))
    cfg, _ = load_config(cfg_path)
    frame = FeatureBuilder(train, cfg).build(
        issue_days_between(date(2023, 2, 1), date(2023, 8, 1), 7)
    )
    assert "wx_nb_fr_wind_cf" in frame.groups["nachbarwetter"]
    reg = Registry(str(tmp_path / "registry"), cfg)
    model = TwoStageModel(cfg, cfg.variant("nb")).fit(frame)
    version = reg.register(model, {"snapshot_id": "s", "clustering_version": "c"}, None, "champion")
    reg.load(version, tmp_path / "cache")
    wx = yaml.safe_load((ROOT / "config/process/gfs_0p5.yaml").read_text())
    nb_wx = {"technologies": wx["technologies"], "pv": wx["pv"]}
    common: dict[str, Any] = {
        "cfg": cfg,
        "config_yaml": cfg_path.read_bytes(),
        "model_dir": tmp_path / "cache" / version,
        "manifest": reg.manifest(version),
        "capacity": snap.capacity,
        "clusters": pd.DataFrame({"technology": ["wind_on"], "cluster_no": [0], "lat": [50.0]}),
        "cluster_capacity": pd.DataFrame(),
        "wx_process": {"technologies": {}},
    }
    with pytest.raises(ValueError, match="Zonen-Gewichte"):
        write_package(tmp_path / "ohne", **common)
    cells = neighbour_cells(NB, REGIONS)
    with pytest.raises(ValueError, match="keine Zellen"):
        write_package(
            tmp_path / "halb",
            **common,
            neighbour_zone_cells=cells[cells["zone"] == "FR"],
            neighbour_wx=nb_wx,
        )
    out = tmp_path / "paket"
    info = write_package(out, **common, neighbour_zone_cells=cells, neighbour_wx=nb_wx)
    assert info["neighbour_weather"]["zones"] == ["FR", "DK1"]
    assert {"neighbour_cells.parquet", "neighbour_wx.yaml"} <= set(info["files"])
    pkg = load_package(out)
    assert pkg.neighbour_cells is not None and set(pkg.neighbour_cells["zone"]) == {"FR", "DK1"}

    class Weather:
        def __init__(self, rows: pd.DataFrame) -> None:
            self.rows = rows

        def update(self, package: Any) -> pd.DataFrame:
            return self.rows

    issue = datetime(2023, 9, 20, 8, tzinfo=UTC)
    cache = EnergyCache(tmp_path, {"smard": FakeSource("smard", snap.energy)})
    with_nb = Predictor(lambda: pkg, cache, lambda p: Weather(train.weather)).run(issue)  # type: ignore[arg-type, return-value]
    assert "nachbarwetter" not in with_nb["inputs_missing"]
    assert any(s["code"] == "natural_earth" for s in with_nb["sources"])
    assert with_nb["model"]["neighbour_weather"]["zones"] == ["FR", "DK1"]
    assert with_nb["targets"]["da_price_de_lu"]
    without = Predictor(lambda: pkg, cache, lambda p: Weather(w)).run(issue)  # type: ignore[arg-type, return-value]
    assert "nachbarwetter" in without["inputs_missing"]
    assert without["targets"]["da_price_de_lu"]

    # Der Wetter-Cache entsteht je Paketversion neu, nicht bei jedem Lauf
    built: list[str] = []

    def factory(p: Any) -> Any:
        built.append(p.version)
        return Weather(train.weather)

    other = load_package(out)
    other.info = {**other.info, "version": "andere"}
    current = [pkg]
    predictor = Predictor(lambda: current[0], cache, factory)
    predictor.run(issue)
    predictor.run(issue)
    current[0] = other
    predictor.run(issue)
    assert built == [pkg.version, "andere"]


def test_oci_pull_and_refresh(package: Path, tmp_path: Path) -> None:
    """@relation(SWR-PRD-011, scope=function)"""
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
    """@relation(SWR-PRD-003, scope=function)"""
    now = datetime(2026, 10, 24, 18, 25, tzinfo=UTC)
    assert next_slot(now, 60, 20) == datetime(2026, 10, 24, 19, 20, tzinfo=UTC)
    assert next_slot(now.replace(minute=5), 60, 20) == datetime(2026, 10, 24, 18, 20, tzinfo=UTC)
    assert next_slot(now, 15, 5) == datetime(2026, 10, 24, 18, 35, tzinfo=UTC)


def test_neighbour_capacity_routes() -> None:
    """Die Leistung der Nachbarländer für ``nachbarindex`` kommt von Energy-Charts.

    @relation(SWR-PRD-020, scope=function)
    """
    from pdb_predict.energy import routes

    r = routes()
    assert r["cap_solar_fr"] == [("energy_charts", "installed_fr")]
    assert r["cap_wind_offshore_dk"] == [("energy_charts", "installed_dk")]
