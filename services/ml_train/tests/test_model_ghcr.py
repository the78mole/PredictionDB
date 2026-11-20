"""Modelle und Registry-Stand in GHCR: Actions registriert, die VM importiert."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from ml_train import model_ghcr
from ml_train.config import load_config
from ml_train.registry import Registry
from wx_exchange.oci import Registry as Oci
from wx_exchange.testing import FakeRegistry

ROOT = Path(__file__).resolve().parents[3]


def _oci(fake: FakeRegistry) -> Oci:
    client = httpx.Client(transport=httpx.MockTransport(fake))
    return Oci(fake.host, fake.repository, client, "bot", "secret", (0.0, 0.0))


def _backtest() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    n = 48
    ts = pd.date_range("2025-01-02", periods=n, freq="h", tz="UTC")
    rows = []
    for target, scale in (("da_price_de_lu", 20.0), ("load_actual", 2000.0)):
        actual = rng.normal(100, 10, n) * scale / 20
        for lead in (1, 2):
            q50 = actual + scale * (1 if lead == 1 else 2) * rng.choice([-1, 1], n)
            rows.append(
                pd.DataFrame(
                    {
                        "issue_time": pd.Timestamp("2025-01-01T08", tz="UTC"),
                        "ts": ts,
                        "lead_day": lead,
                        "q10": q50 - 5,
                        "q50": q50,
                        "q90": q50 + 5,
                        "actual": actual,
                        "target": target,
                        "fold": 0,
                    }
                )
            )
    return pd.concat(rows, ignore_index=True)


def _version(tmp: Path, version: str, variant: str, *, publishable: bool = True) -> Path:
    d = tmp / version
    d.mkdir(parents=True)
    (d / "model.json").write_text(json.dumps({"variant": {"name": variant}}))
    (d / "stage1_load_actual_q50.txt").write_text("tree")
    _backtest().to_parquet(d / "backtest.parquet", index=False)
    man = {
        "config": "de_lu_v2",
        "version": version,
        "variant": {"name": variant},
        "snapshot_id": "abc123",
        "publishable": publishable,
        "sources": {"smard": "frei"},
        "train_issue_days": ["2021-04-01", "2026-11-10"],
        "git_commit": "deadbeef",
        "files": ["model.json", "stage1_load_actual_q50.txt", "backtest.parquet", "manifest.json"],
    }
    (d / "manifest.json").write_text(json.dumps(man))
    return d


@pytest.fixture
def cfg():  # type: ignore[no-untyped-def]
    return load_config(ROOT / "config/ml/de_lu_v2.yaml")[0]


def test_push_import_roundtrip(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-080, SWR-MLT-081, scope=function)"""
    fake = FakeRegistry(repository="owner/predictiondb-models/de_lu")
    oci = _oci(fake)
    actions = Registry(str(tmp_path / "actions"), cfg)
    v = "de_lu_v2-frei-202611180300-abc123"
    actions.import_version(_version(tmp_path / "src", v, "frei"))
    assert actions.state()["challengers"] == [v]
    tag = model_ghcr.challenger_tag(date(2026, 11, 18), cfg.name, "frei")
    model_ghcr.push_version(oci, actions, v, [tag])
    assert set(fake.manifests) == {v, tag}
    m = fake.manifest(v)
    assert m["artifactType"] == model_ghcr.ARTIFACT_MODEL
    notes = m["annotations"]
    assert notes["io.predictiondb.variant"] == "frei"
    assert float(notes["io.predictiondb.metric.da_price_de_lu.d1.mae"]) == pytest.approx(20.0)
    assert "Preis MAE D+1 20,0, D+2 40,0 €/MWh" in notes[model_ghcr.DESCRIPTION]
    assert "Last MAE D+1 2,0, D+2 4,0 GW" in notes[model_ghcr.DESCRIPTION]
    assert len(notes[model_ghcr.DESCRIPTION]) <= model_ghcr.DESCRIPTION_LIMIT

    vm = Registry(str(tmp_path / "vm"), cfg)
    res = model_ghcr.import_versions(oci, vm)
    assert res.imported == [v]
    assert vm.state()["challengers"] == [v]
    assert vm.manifest(v)["variant"]["name"] == "frei"
    got = vm.backtest(v)
    assert got is not None and len(got) == len(_backtest())
    # zweiter Import: nichts Neues, Rollen bleiben
    again = model_ghcr.import_versions(oci, vm)
    assert again.imported == [] and again.present == [v]
    assert vm.state()["challengers"] == [v]


def test_import_refuses_unfree_and_completes_partial(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-081, scope=function)"""
    fake = FakeRegistry(repository="owner/predictiondb-models/de_lu")
    oci = _oci(fake)
    src = Registry(str(tmp_path / "src"), cfg)
    v = "de_lu_v2-frei-202611180300-abc123"
    src.import_version(_version(tmp_path / "a", v, "frei"))
    model_ghcr.push_version(oci, src, v)
    model_ghcr.push_version(oci, src, v)  # gleicher Inhalt: erneut erlaubt
    # abgebrochener Import: Manifest da, Rolle fehlt; der nächste Import holt alles nach
    vm = Registry(str(tmp_path / "vm"), cfg)
    vm.store.write(
        f"de_lu_v2/{v}/manifest.json", (tmp_path / "a" / v / "manifest.json").read_bytes()
    )
    assert vm.has(v) and not vm.complete(v)
    assert model_ghcr.import_versions(oci, vm).imported == [v]
    assert vm.complete(v) and vm.state()["challengers"] == [v]
    # anderer Inhalt unter demselben Namen wird nicht überschrieben
    other = Registry(str(tmp_path / "other"), cfg)
    d = _version(tmp_path / "b", v, "frei")
    (d / "stage1_load_actual_q50.txt").write_text("anderer Baum")
    other.import_version(d)
    with pytest.raises(ValueError, match="anderem Inhalt"):
        model_ghcr.push_version(oci, other, v)
    # Artefakt ohne Nachweis freier Quellen (von Hand in GHCR gelegt) wird abgelehnt
    bad = "de_lu_v2-frei-202611190300-abc123"
    unfree = Registry(str(tmp_path / "unfree"), cfg)
    unfree.import_version(_version(tmp_path / "c", bad, "frei", publishable=False))
    files = model_ghcr.version_files(unfree, bad)
    model_ghcr._push(oci, files, model_ghcr.ARTIFACT_MODEL, {}, [bad])
    with pytest.raises(model_ghcr.NotPublishable):
        model_ghcr.import_versions(oci, Registry(str(tmp_path / "vm2"), cfg), [bad])


def test_push_refuses_unfree_snapshot(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-080, scope=function)"""
    reg = Registry(str(tmp_path / "r"), cfg)
    v = "de_lu_v2-frei-202611180300-abc123"
    reg.import_version(_version(tmp_path / "src", v, "frei", publishable=False))
    fake = FakeRegistry(repository="owner/predictiondb-models/de_lu")
    with pytest.raises(model_ghcr.NotPublishable):
        model_ghcr.push_version(_oci(fake), reg, v)
    assert fake.manifests == {}


def test_state_roles_and_tags(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-046, SWR-MLT-082, scope=function)"""
    fake = FakeRegistry(repository="owner/predictiondb-models/de_lu")
    oci = _oci(fake)
    reg = Registry(str(tmp_path / "vm"), cfg)
    a, b = "de_lu_v2-frei-202611010300-aaaaaa", "de_lu_v2-wetter-202611180300-bbbbbb"
    for v, var in ((a, "frei"), (b, "wetter")):
        reg.import_version(_version(tmp_path / "src", v, var))
    model_ghcr.push_version(oci, reg, b)  # nur b liegt in GHCR
    st = reg.state()
    reg._make_champion(st, a, "")
    st["history"][-1]["at"] = "2026-11-02T10:00:00+00:00"
    reg._make_champion(st, b, "")
    st["history"][-1]["at"] = "2026-11-19T10:00:00+00:00"
    reg._save_state(st)
    out = model_ghcr.publish_state(oci, reg)
    # a liegt noch nicht in GHCR, hat aber einen freien Snapshot: wird mit hochgeladen
    assert out["tags"] == {
        "champion-2026-11-02": a,
        "honor-2026-11-19": a,
        "champion-2026-11-19": b,
        "champion": b,
    }
    assert out["skipped"] == {}
    assert fake.manifest("champion")["layers"] == fake.manifest(b)["layers"]
    state = model_ghcr.read_states(oci)["de_lu_v2"]
    assert state["champion"] == b and state["previous"] == [a]
    assert state["variants"] == {a: "frei", b: "wetter"}
    names = [v.name for v in cfg.variants]
    assert model_ghcr.active_variants(state, names) == ["frei", "wetter"]
    assert model_ghcr.roles_by_variant(state) == {"wetter": ["champion"], "frei": ["vorgaenger"]}
    # zweite Konfiguration im selben Repository bleibt beim nächsten Stand erhalten
    files = {
        "registry-de_lu_v1.json": b'{"champion": null}',
        "registry-de_lu_v2.json": b"{}",
    }
    model_ghcr._push(oci, files, model_ghcr.ARTIFACT_STATE, {}, [model_ghcr.STATE_TAG])
    model_ghcr.publish_state(oci, reg)
    assert set(model_ghcr.read_states(oci)) == {"de_lu_v1", "de_lu_v2"}


def test_report_only_metrics(cfg, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """@relation(SWR-MLT-083, scope=function)"""
    fake = FakeRegistry(repository="owner/predictiondb-models/de_lu")
    oci = _oci(fake)
    bt = tmp_path / "bt"
    bt.mkdir()
    (bt / "metrics.csv").write_text("method,target,mae\nfrei,da_price_de_lu,20\n")
    (bt / "lauf.json").write_text(json.dumps({"snapshot_id": "abc", "git_commit": "x"}))
    _backtest().to_parquet(bt / "predictions.parquet")
    state = {"champion": "v1", "challengers": [], "previous": [], "variants": {"v1": "frei"}}
    tags = model_ghcr.push_report(oci, bt, "de_lu_v2", state, date(2026, 11, 18))
    assert tags == ["backtest-de_lu_v2", "backtest-de_lu_v2-2026-11-18"]
    files = model_ghcr.pull_report(oci, "de_lu_v2")
    assert files is not None
    assert set(files) == {"metrics.csv", "lauf.json", "rollen.json"}
    assert json.loads(files["rollen.json"])["roles"] == {"frei": ["champion"]}
    with pytest.raises(FileNotFoundError):
        model_ghcr.push_report(oci, tmp_path, "de_lu_v2", None)


def test_tags_follow_oci_rules() -> None:
    """@relation(SWR-MLT-080, scope=function)"""
    t = model_ghcr.challenger_tag(date(2026, 11, 18), "de_lu_v2", "wetter_frei_gk_skaliert_gefs")
    assert t == "challenger-2026-11-18-de_lu_v2-wetter_frei_gk_skaliert_gefs"
    with pytest.raises(ValueError):
        model_ghcr.check_tag("a/b")


def test_pages_model_table() -> None:
    """Kachel der Pages-Seite: Rollen zuerst, naive Verfahren zuletzt, Last in GW.

    @relation(SWR-MLT-083, scope=function)
    """
    import importlib
    import sys

    sys.path.insert(0, str(ROOT / "pages"))
    modelle = importlib.import_module("modelle")

    rows = []
    for method, price, load in (
        ("frei", 20.0, 1500.0),
        ("wetter", 18.0, 1200.0),
        ("naive_day", 30.0, 3000.0),
        ("bench_load_da_forecast", float("nan"), 1400.0),
    ):
        for lead in (1, 2):
            rows.append(("w2024_2025", method, "da_price_de_lu", lead, price * lead))
            rows.append(("w2024_2025", method, "load_actual", lead, load * lead))
    rows += [("w2024_2025", "teilweise", "da_price_de_lu", 1, 5.0)]
    csv = pd.DataFrame(rows, columns=["window", "method", "target", "lead_day", "mae"])
    csv["complete"] = csv["method"] != "teilweise"
    files = {
        "metrics_window_lead.csv": csv.to_csv(index=False).encode(),
        "lauf.json": json.dumps(
            {
                "compare": {"name": "w2024_2025", "start": "2024-01-01", "end": "2025-12-31"},
                "snapshot_id": "abc",
                "finished": "2026-11-18T12:00:00+00:00",
            }
        ).encode(),
        "rollen.json": json.dumps(
            {"roles": {"frei": ["champion"], "wetter": ["challenger"]}}
        ).encode(),
    }
    out = modelle.tabelle(files)
    assert out is not None
    tab, unter = out
    order = [tab.index(x) for x in (">frei<", ">wetter<", ">naive_day<")]
    assert order == sorted(order)
    assert "bench_" not in tab and "teilweise" not in tab
    assert "<td>20,0</td>" in tab and "<td>1,2</td>" in tab and "<td>&ndash;</td>" in tab
    assert "2024-01-01 bis 2025-12-31" in unter
    # Fehlerkurve je Zeile für Preis und Last, groß mit Wert je Vorlauftag
    assert tab.count('class="kurve"') == 2 * 3
    assert "<title>D+2: 36,0 €/MWh</title>" in tab and "<title>D+2: 2,4 GW</title>" in tab
    assert "kein Bericht" in modelle.kachel(None)
