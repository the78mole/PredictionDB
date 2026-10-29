import gzip
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import obstore
import pytest
from obstore.store import MemoryStore

from gribkit.grib import describe, iter_messages
from wx_exchange import gefs, gefs_vm
from wx_exchange.gefs import (
    VARIABLES,
    FieldSpec,
    GefsConfig,
    GefsMonth,
    GefsPublisher,
    Message,
    Regions,
    RunBuilder,
    decode,
    fingerprint,
    pack,
    ranges,
    select,
    unpack,
)
from wx_exchange.gefs_regions import area_cells, inside, mastr_regions
from wx_exchange.oci import Registry
from wx_exchange.testing import FakeRegistry
from wx_process.features import PowerCurve
from wx_process.testing import _message, grid

RUN = datetime(2099, 1, 2, tzinfo=UTC)
LAT, LON = grid(north=56, south=46, west=4, east=16, res=0.5)
TMP = (0, 0, 0, 103, 2, "instant")

#: Feld → (Produkt, GRIB-Kürzel, Ebene, Wert)
FIELDS: dict[str, tuple[str, str, str, float]] = {
    "t2m": ("a", "TMP", "2 m above ground", 290.0),
    "u10": ("a", "UGRD", "10 m above ground", 3.0),
    "v10": ("a", "VGRD", "10 m above ground", 4.0),
    "tp": ("a", "APCP", "surface", 6.0),
    "tcc": ("a", "TCDC", "entire atmosphere", 40.0),
    "dswrf": ("a", "DSWRF", "surface", 500.0),
    "u100": ("b", "UGRD", "100 m above ground", 6.0),
    "v100": ("b", "VGRD", "100 m above ground", 8.0),
}
#: in f000 gibt es keine Mittel und Summen
NO_F000 = {"tp", "tcc", "dswrf"}


def make_config(tmp_path: Path, **kw: Any) -> GefsConfig:
    regions = {
        "version": "t1",
        "regions": [
            {
                "code": "DE:wind_on",
                "technology": "wind_on",
                "hub_height_m": 120,
                "cells": [[52.0, 10.0, 3.0], [53.0, 9.5, 1.0]],
            },
            {"code": "DE:pv", "technology": "pv", "cells": [[50.0, 8.0, 1.0]]},
            {
                "code": "XX:area",
                "technology": "area",
                "cells": [[48.0, 12.0, 0.5], [48.5, 12.0, 0.5]],
            },
        ],
    }
    path = tmp_path / "regions.json.gz"
    path.write_bytes(gzip.compress(json.dumps(regions).encode(), mtime=0))
    data: dict[str, Any] = {
        "base_url": "https://gefs.test",
        "first_run": "2099-01-01T00:00:00Z",
        "perturbed": 1,
        "steps": {"start": 0, "end": 12, "every": 6},
        "fields": {
            k: {"product": p, "param": a, "level": lv} for k, (p, a, lv, _) in FIELDS.items()
        },
        "bbox": {"north": 55, "south": 47, "west": 5, "east": 15},
        "regions": str(path),
        "concurrency": 4,
        "technologies": {
            "wind_on": {"kind": "wind", "hub_height_m": 100},
            "pv": {"kind": "pv"},
            "area": {"kind": "other"},
        },
    }
    data.update(kw)
    return GefsConfig.model_validate(data)


class FakeArchive:
    """AWS-Archiv mit ``.idx`` und Byte-Bereichen; ``gone`` = fehlende (Lauf, Mitglied, Schritt)."""

    def __init__(
        self,
        config: GefsConfig,
        runs: list[datetime],
        gone: frozenset[tuple[datetime, str, int]] = frozenset(),
    ) -> None:
        self.files: dict[str, bytes] = {}
        self.requests: list[httpx.Request] = []
        for run in runs:
            for member in config.members:
                for step in config.steps.list():
                    if (run, member, step) in gone:
                        continue
                    for product in ("a", "b"):
                        self._file(config, run, member, step, product)

    def _file(
        self, config: GefsConfig, run: datetime, member: str, step: int, product: str
    ) -> None:
        data, idx = b"", []
        # vorweg eine fremde Nachricht, damit die Auswahl Versätze ungleich 0 trifft
        names = ["other", *(k for k, v in FIELDS.items() if v[0] == product)]
        for n, name in enumerate(names, 1):
            if step == 0 and name in NO_F000:
                continue
            param, level, value = ("HGT", "500 mb", 1.0) if name == "other" else FIELDS[name][1:]
            idx.append(f"{n}:{len(data)}:d={run:%Y%m%d%H}:{param}:{level}:{step} hour fcst:ENS=x")
            values = np.full((len(LAT), len(LON)), value)
            data += _message(TMP, LAT, LON, values, 0, step)
        path = "/" + config.url(run, member, step, product).split("/", 3)[3]
        self.files[path] = data
        self.files[path + ".idx"] = ("\n".join(idx) + "\n").encode()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        data = self.files.get(request.url.path)
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if rng:
            a, _, b = rng.removeprefix("bytes=").partition("-")
            data = data[int(a) : int(b) + 1 if b else None]
            return httpx.Response(206, content=data)
        return httpx.Response(200, content=data)


def client(archive: FakeArchive) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(archive))


def registry(fake: FakeRegistry) -> Registry:
    c = httpx.Client(transport=httpx.MockTransport(fake))
    return Registry(fake.host, fake.repository, c, "bot", "secret", (0.0, 0.0))


IDX = """1:0:d=2099010200:HGT:500 mb:6 hour fcst:ENS=+1
2:100:d=2099010200:TMP:2 m above ground:6 hour fcst:ENS=+1
3:250:d=2099010200:UGRD:10 m above ground:6 hour fcst:ENS=+1
4:400:d=2099010200:VGRD:10 m above ground:6 hour fcst:ENS=+1
5:550:d=2099010200:APCP:surface:0-6 hour acc fcst:ENS=+1
"""


def test_select_and_ranges() -> None:
    wanted = {
        "t2m": FieldSpec(product="a", param="TMP", level="2 m above ground"),
        "v10": FieldSpec(product="a", param="VGRD", level="10 m above ground"),
        "u10": FieldSpec(product="a", param="UGRD", level="10 m above ground"),
        "tp": FieldSpec(product="a", param="APCP", level="surface"),
        "nope": FieldSpec(product="a", param="RH", level="2 m above ground"),
    }
    msgs = select(IDX, wanted)
    assert msgs == [
        Message("t2m", 100, 250),
        Message("u10", 250, 400),
        Message("v10", 400, 550),
        Message("tp", 550, None),
    ]
    # alles hängt aneinander: eine Anfrage bis Dateiende
    assert [[m.name for m in g] for g in ranges(msgs)] == [["t2m", "u10", "v10", "tp"]]
    gaps = [Message("a", 0, 10), Message("b", 20, 30), Message("c", 30, 40)]
    assert [[m.name for m in g] for g in ranges(gaps)] == [["a"], ["b", "c"]]


def test_config_members_urls_and_runs(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert cfg.members == ["gec00", "gep01"]
    assert cfg.url(RUN, "gep01", 6, "b") == (
        "https://gefs.test/gefs.20990102/00/atmos/pgrb2bp5/gep01.t00z.pgrb2b.0p50.f006"
    )
    runs = cfg.runs_between(
        datetime(2098, 12, 1, tzinfo=UTC), datetime(2099, 1, 3, 5, tzinfo=UTC), [0]
    )
    assert runs == [datetime(2099, 1, d, tzinfo=UTC) for d in (1, 2, 3)]


def test_repository_config_loads() -> None:
    root = Path(__file__).resolve().parents[3]
    cfg = GefsConfig.from_yaml(root / "config/sources/gefs_0p5.yaml")
    assert len(cfg.members) == 31 and len(cfg.steps.list()) == 65
    assert cfg.first_run == datetime(2021, 3, 22, 12, tzinfo=UTC)
    regions = Regions.load(cfg.regions)
    assert {
        "DE:wind_on",
        "DE:wind_off",
        "DE:pv",
        "DE:area",
        "ES:area",
        "TR:area",
        "NO4:area",
    } <= set(regions.codes)
    for r in regions.regions:
        assert r.share.sum() == pytest.approx(1.0)
        assert (r.lat <= cfg.bbox.north).all() and (r.lat >= cfg.bbox.south).all()
        assert (r.lon >= cfg.bbox.west).all() and (r.lon <= cfg.bbox.east).all()


async def test_build_run_aggregates_per_region_and_member(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    regions = Regions.load(cfg.regions)
    archive = FakeArchive(cfg, [RUN], gone=frozenset({(RUN, "gep01", 12)}))
    async with client(archive) as c:
        silver = await RunBuilder(cfg, regions, c).build(RUN)

    assert silver.values.shape == (2, 3, 3, len(VARIABLES))
    assert silver.missing == 1 and silver.failed == 0
    v = {name: silver.values[..., k] for k, name in enumerate(VARIABLES)}
    np.testing.assert_allclose(v["t2m"][0, 1], 290.0 - 273.15, atol=1e-3)
    np.testing.assert_allclose(v["wind_speed_100m"][0, 1], 10.0, atol=1e-3)
    # Nabenhöhe 120 m mit Hellmann-Exponent aus 10 m (5 m/s) und 100 m (10 m/s)
    alpha = np.log(2.0) / np.log(10.0)
    expected = float(PowerCurve()(np.array([10.0 * 1.2**alpha]))[0])
    assert v["wind_cf"][0, 1, 0] == pytest.approx(expected, abs=1e-4)
    np.testing.assert_allclose(v["ghi"][0, 1], 500.0, atol=0.1)
    np.testing.assert_allclose(v["precip"][0, 1], 1.0, atol=1e-3)
    np.testing.assert_allclose(v["cloud_cover"][0, 1], 40.0, atol=1e-2)
    assert ((v["pv_cf"][0, 1] > 0) & (v["pv_cf"][0, 1] < 1)).all()
    # f000 ohne Mittel und Summen, fehlende Datei bleibt NaN
    assert np.isnan(v["ghi"][:, 0]).all() and np.isfinite(v["t2m"][:, 0]).all()
    assert np.isnan(silver.values[1, 2]).all() and np.isfinite(silver.values[0, 2, :, 0]).all()
    # je Datei .idx plus eine Anfrage je zusammenhängendem Block
    assert all(r.headers.get("range") for r in archive.requests if not r.url.path.endswith(".idx"))


def test_pack_is_reproducible() -> None:
    values = np.arange(24, dtype=np.float32).reshape(1, 2, 3, 4)
    values[0, 0, 0, 0] = np.nan
    meta = {"run": RUN.isoformat(), "b": 1, "a": [1, 2]}
    data, h = pack(meta, values)
    again, h2 = pack(dict(reversed(meta.items())), values.copy())
    assert data == again and h == h2
    meta2, values2 = unpack(data)
    assert meta2 == meta
    np.testing.assert_array_equal(values2, values)
    assert pack(meta, values + 1)[1] != h


def test_fingerprint_changes_with_inputs(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    regions = Regions.load(cfg.regions)
    fp = fingerprint(cfg, regions)
    assert fingerprint(make_config(tmp_path), regions) == fp
    assert fingerprint(make_config(tmp_path, perturbed=2), regions) != fp
    other = Regions(regions.version, regions.regions, "0" * 16)
    assert fingerprint(cfg, other) != fp


async def test_publish_month_uploads_skips_and_records_missing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    regions = Regions.load(cfg.regions)
    d1, d2, d3 = (datetime(2099, 1, d, tzinfo=UTC) for d in (1, 2, 3))
    archive = FakeArchive(cfg, [d1, d2])  # 3. fehlt ganz
    fake = FakeRegistry()

    def publisher(c: httpx.AsyncClient, now: datetime) -> GefsPublisher:
        return GefsPublisher(
            registry(fake),
            cfg,
            regions,
            c,
            source_url="https://github.com/o/r",
            parallel_runs=2,
            now=now,
        )

    async with client(archive) as c:
        # der 3. ist noch jung: ausstehend, nicht fehlend
        res = await publisher(c, d3 + timedelta(hours=10)).publish_month("2099-01", 0, d1, d3)
        assert res.runs == {
            "2099010100": "uploaded",
            "2099010200": "uploaded",
            "2099010300": "pending",
        }
        month = GefsMonth.from_manifest("2099-01", 0, fake.manifest("2099-01-00z"))
        assert set(month.layers) == {d1, d2} and month.missing == set()
        assert month.regions_version == "t1"
        meta, values = unpack(fake.blobs[month.layers[d2].digest])
        assert meta["members"] == ["gec00", "gep01"] and meta["regions"] == regions.codes
        assert values.shape == (2, 3, 3, len(VARIABLES))

        # später: der 3. gilt als fehlend, die beiden anderen werden nicht neu geladen
        n = len(archive.requests)
        res = await publisher(c, d3 + timedelta(days=5)).publish_month("2099-01", 0, d1, d3)
        assert res.runs == {"2099010300": "missing"} and res.skipped == 2
        assert len(archive.requests) - n == 6  # je Mitglied-Schritt nur die erste .idx
        month = GefsMonth.from_manifest("2099-01", 0, fake.manifest("2099-01-00z"))
        assert month.missing == {d3}

        # Neubau mit gleichem Inhalt: kein Upload, kein neues Manifest
        blobs, manifests = fake.blob_puts(), fake.manifest_puts()
        res = await publisher(c, d3 + timedelta(days=5)).publish_month(
            "2099-01", 0, d1, d3, rebuild=True
        )
        assert res.runs == {"2099010100": "unchanged", "2099010200": "unchanged"}
        assert fake.blob_puts() == blobs and fake.manifest_puts() == manifests

    # Plan: nur noch der Rest des Monats offen
    rows = gefs.plan(registry(fake), cfg, regions, d1, datetime(2099, 1, 5, tzinfo=UTC))
    assert rows == [{"month": "2099-01", "hour": 0, "todo": 2}]


def test_inside_and_area_cells() -> None:
    square = [np.array([[5.0, 47.0], [7.0, 47.0], [7.0, 49.0], [5.0, 49.0], [5.0, 47.0]])]
    lon, lat = np.array([6.0, 8.0, 5.5]), np.array([48.0, 48.0, 50.0])
    assert inside(lon, lat, square).tolist() == [True, False, False]
    bbox = gefs.BBox(north=55, south=45, west=0, east=20)
    cells = area_cells(square, bbox, 0.5)
    assert sum(c[2] for c in cells) == pytest.approx(1.0, abs=1e-5)
    # Randzellen liegen halb, Eckzellen zu einem Viertel im Quadrat
    by = {(c[0], c[1]): c[2] for c in cells}
    assert by[(48.0, 6.0)] == pytest.approx(2 * by[(48.0, 5.0)], rel=0.02)
    assert by[(48.0, 6.0)] == pytest.approx(4 * by[(47.0, 5.0)], rel=0.03)
    assert (49.5, 6.0) not in by


def test_mastr_regions(tmp_path: Path) -> None:
    path = tmp_path / "cap.csv"
    path.write_text(
        "technology,lat,lon,mw,hub_m,units,export_date\n"
        "wind_on,52.0,10.0,30,100,3,2026-10-20\n"
        "wind_on,53.0,9.5,10,140,1,2026-10-20\n"
        "pv_ground,50.0,8.0,5,,10,2026-10-20\n"
        "pv_roof,50.0,8.0,15,,100,2026-10-20\n"
        "biomass,50.0,8.0,15,,1,2026-10-20\n"
    )
    regions, export = mastr_regions(path)
    assert export == "2026-10-20"
    by = {r["code"]: r for r in regions}
    assert by["DE:wind_on"]["capacity_mw"] == 40 and by["DE:wind_on"]["hub_height_m"] == 110
    assert by["DE:wind_on"]["cells"] == [[52.0, 10.0, 0.75], [53.0, 9.5, 0.25]]
    assert by["DE:pv"]["capacity_mw"] == 20 and by["DE:pv"]["cells"] == [[50.0, 8.0, 1.0]]
    assert "hub_height_m" not in by["DE:pv"]


# --- VM: live mit Bronze, Import, Aufräumen -------------------------------------------------


def vm_for(
    tmp_path: Path, archive: FakeArchive, fake: FakeRegistry, c: httpx.AsyncClient, now: datetime
) -> gefs_vm.GefsVm:
    cfg = make_config(tmp_path)
    return gefs_vm.GefsVm(
        registry(fake),
        cfg,
        Regions.load(cfg.regions),
        c,
        bronze=gefs_vm.GefsBronze(MemoryStore(), cfg.bbox),
        local=gefs_vm.GefsLocal(MemoryStore()),
        ledger=None,
        source_url="https://github.com/o/r",
        history_start=datetime(2099, 1, 1, tzinfo=UTC),
        bronze_days=10,
        catch_up_days=1,
        now=lambda: now,
    )


async def test_vm_builds_current_month_with_bronze_and_imports_history(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    jan = [datetime(2099, 1, d, tzinfo=UTC) for d in (30, 31)]
    feb = [datetime(2099, 2, d, tzinfo=UTC) for d in (1, 2)]
    archive = FakeArchive(cfg, jan + feb)
    fake = FakeRegistry()
    now = datetime(2099, 2, 2, 12, tzinfo=UTC)
    async with client(archive) as c:
        # Januar wie aus Actions: nur nach GHCR
        pub = GefsPublisher(
            registry(fake), cfg, Regions.load(cfg.regions), c, source_url="x", now=now
        )
        await pub.publish_month("2099-01", 0, jan[0], jan[-1])
        vm = vm_for(tmp_path, archive, fake, c, now)
        states: list[tuple[datetime, str, int | None]] = []
        vm.bronze.on_state = lambda r, st, n: states.append((r, st, n))
        result = await vm.cycle()

    # Status je Lauf für das Dashboard: erst laden, dann vorhanden mit Bytes
    assert [(r, st) for r, st, _ in states if st == "loading"] == [(r, "loading") for r in feb]
    present = {r: n for r, st, n in states if st == "present"}
    assert set(present) == set(feb) and all(n and n > 0 for n in present.values())
    # aktueller Monat selbst gebaut, mit Bronze; der 31.01. lag schon in GHCR (sonst baut die
    # VM die letzten Tage des Vormonats mit), Januar wird importiert
    assert vm.bronze.runs() == feb
    local = vm.local.runs()
    assert {r for r, i in local.items() if i.origin == "bronze"} == set(feb)
    assert {r for r, i in local.items() if i.origin == "ghcr"} == set(jan)
    assert result.imported["imported"] == 2
    # lokales Silber ist byte-gleich mit dem Layer in GHCR
    for month, runs in (("2099-01", jan), ("2099-02", feb)):
        art = GefsMonth.from_manifest(month, 0, fake.manifest(f"{month}-00z"))
        for run in runs:
            assert art.layers[run].content == local[run].content
    assert vm.local.load(feb[0]) == fake.blobs[local[feb[0]].digest]
    # Bronze: je Mitglied-Schritt eine zugeschnittene Datei plus run.json
    keys = [
        m["path"]
        for b in obstore.list(vm.bronze.store, prefix="gefs/0p5/2099/02/01/00/")
        for m in b
    ]
    assert len(keys) == 2 * 3 + 1
    cropped = obstore.get(vm.bronze.store, "gefs/0p5/2099/02/01/00/gec00.f006.grib2").bytes()
    msgs = list(iter_messages(bytes(cropped)))
    assert len(msgs) == len(FIELDS)  # Produkte a und b zusammen
    # auf den Ausschnitt 55..47 °N, 5..15 °O zugeschnitten (Archiv: 56..46 °N, 4..16 °O)
    assert decode({"x": msgs[0]}, cfg.bbox)[1]["x"].shape == (17, 21)
    assert describe(msgs[0]).nj == 17 and describe(msgs[0]).ni == 21

    # zweiter Durchgang: nichts mehr zu laden
    n = len(archive.requests)
    async with client(archive) as c:
        vm2 = vm_for(tmp_path, archive, fake, c, now)
        vm2.bronze, vm2.local = vm.bronze, vm.local
        again = await vm2.cycle()
    assert again.imported["imported"] == 0 and len(archive.requests) == n


async def test_vm_pending_run_is_not_requested(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    archive = FakeArchive(cfg, [])
    async with client(archive) as c:
        vm = vm_for(tmp_path, archive, FakeRegistry(), c, datetime(2099, 2, 1, 5, tzinfo=UTC))
        vm.catch_up_days = 0
        live = await vm.live()
    assert live[0]["counts"] == {"pending": 1}
    # nur eine Probe auf die .idx des letzten Mitglied-Schritts
    assert [r.method for r in archive.requests] == ["HEAD"]


async def test_clean_bronze_only_when_silver_matches(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    runs = [datetime(2099, 2, d, tzinfo=UTC) for d in (1, 2)]
    archive = FakeArchive(cfg, runs)
    fake = FakeRegistry()
    async with client(archive) as c:
        vm = vm_for(tmp_path, archive, fake, c, datetime(2099, 2, 2, 12, tzinfo=UTC))
        await vm.live()
        # 20 Tage später, Bronze-Frist 10 Tage; beim 2. weicht das lokale Silber ab
        vm.now = lambda: datetime(2099, 2, 22, tzinfo=UTC)
        info = vm.local.info(runs[1])
        assert info is not None
        vm.local.save(runs[1], b"x", gefs_vm.LocalInfo(**{**info.__dict__, "content": "anders"}))
        assert vm.clean_bronze() == (1, 1)
    assert vm.bronze.runs() == [runs[1]]


def test_ensemble_stats() -> None:
    rng = np.random.default_rng(0)
    values = rng.normal(10, 2, size=(31, 3, 2, 1)).astype(np.float32)
    values[:, 0, 1, 0] = np.nan  # f000 ohne Mittel
    values[:25, 2, 0, 0] = np.nan  # zu wenige Mitglieder
    s = gefs_vm.ensemble_stats(values)
    assert s["mean"][1, 0, 0] == pytest.approx(values[:, 1, 0, 0].mean(), rel=1e-5)
    assert s["p10"][1, 0, 0] < s["mean"][1, 0, 0] < s["p90"][1, 0, 0]
    assert np.isnan(s["std"][0, 1, 0]) and np.isnan(s["mean"][2, 0, 0])


async def test_vm_cycle_imports_history_before_live(tmp_path: Path) -> None:
    """Der erste Aufbau des laufenden Monats dauert Stunden, die Historie soll nicht warten."""
    cfg = make_config(tmp_path)
    archive = FakeArchive(cfg, [])
    now = datetime(2099, 2, 2, 12, tzinfo=UTC)
    async with client(archive) as c:
        vm = vm_for(tmp_path, archive, FakeRegistry(), c, now)
        order: list[str] = []

        def imp() -> Counter[str]:
            order.append("import")
            return Counter()

        async def live() -> list[dict[str, Any]]:
            order.append("live")
            return []

        vm.import_history = imp  # type: ignore[method-assign]
        vm.live = live  # type: ignore[method-assign]
        await vm.cycle()
    assert order == ["import", "live"]
