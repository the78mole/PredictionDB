"""MLflow-Anbindung gegen einen lokalen Dateispeicher (ohne Server).

@relation(SWA-MLT-TRACKING, scope=file)
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ml_train import tracking
from ml_train.config import load_config
from ml_train.registry import Registry

ROOT = Path(__file__).resolve().parents[3]
CFG_PATH = ROOT / "config/ml/de_lu_v1.yaml"
# Der Dateispeicher von MLflow nutzt codecs.open (unter Python 3.14 abgekündigt); auf der VM
# spricht ml-train mit dem Server, nicht mit dem Dateispeicher
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def mlflow_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "mlruns"
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"file://{d}")
    # Registry sonst als sqlite im Arbeitsverzeichnis
    monkeypatch.setenv("MLFLOW_REGISTRY_URI", f"sqlite:///{tmp_path / 'registry.db'}")
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    monkeypatch.setenv("MLFLOW_DISABLE_AGENT_HINT", "1")
    return d


def _backtest_dir(out: Path) -> None:
    """Ordner wie aus ``ml-train backtest``, nur mit den Dateien, die die Anbindung liest."""
    out.mkdir()
    rows = []
    for method in ("kalender", "naive_day"):
        for lead in (1, 2):
            rows.append(
                {
                    "method": method,
                    "target": "load_actual",
                    "lead_day": lead,
                    "n": 48,
                    "mae": 1000.0 + lead + (method == "naive_day") * 500,
                    "rmse": 1300.0,
                    "bias": -5.0,
                    "pinball": float("nan"),
                    "coverage80": 0.8,
                }
            )
    lead = pd.DataFrame(rows)
    total = lead.groupby(["method", "target"], as_index=False).agg(
        {"n": "sum", "mae": "mean", "rmse": "mean", "bias": "mean", "coverage80": "mean"}
    )
    for stem in ("metrics", "metrics_window"):
        lead.to_csv(out / f"{stem}_lead.csv", index=False)
        total.to_csv(out / f"{stem}.csv", index=False)
    run = {
        "snapshot_id": "abc123",
        "config_sha256": "f00",
        "compare": {"name": "w2024_2025", "complete": ["kalender"], "incomplete": ["naive_day"]},
        "compare_rolling": None,
        "folds": [
            {"no": 0, "test_from": "2024-01-01", "test_to": "2024-06-30"},
            {"no": 1, "test_from": "2024-07-01", "test_to": "2024-12-31"},
        ],
    }
    (out / "lauf.json").write_text(json.dumps(run))


def test_disabled_without_tracking_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-MLT-047, scope=function)"""
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    cfg, _ = load_config(CFG_PATH)
    assert tracking.log_backtest(cfg, CFG_PATH, tmp_path, {}) is None
    assert tracking.mirror_registry(Registry(str(tmp_path), cfg)) == 0


def test_metric_names() -> None:
    """@relation(SWR-MLT-047, scope=function)"""
    lead = pd.DataFrame(
        [{"target": "da_price_de_lu", "lead_day": 1, "mae": 9.5, "n": 3, "pinball": float("nan")}]
    )
    total = pd.DataFrame([{"target": "da_price_de_lu", "mae": 10.0, "n": 6}])
    m = tracking.metric_items(lead, total, "w.")
    assert m == {
        "w.da_price_de_lu.d1.mae": 9.5,
        "w.da_price_de_lu.d1.n": 3.0,
        "w.da_price_de_lu.all.mae": 10.0,
        "w.da_price_de_lu.all.n": 6.0,
    }


def test_backtest_parent_and_method_runs(tmp_path: Path, mlflow_dir: Path) -> None:
    """@relation(SWR-MLT-047, scope=function)"""
    from mlflow import MlflowClient

    cfg, _ = load_config(CFG_PATH)
    out = tmp_path / "bt"
    _backtest_dir(out)
    rid = tracking.log_backtest(cfg, CFG_PATH, out, {"variant": ["kalender"], "rolling": True})
    assert rid
    c = MlflowClient()
    parent = c.get_run(rid)
    assert parent.data.tags["snapshot_id"] == "abc123"
    assert parent.data.tags["test_from"] == "2024-01-01"
    assert parent.data.tags["test_to"] == "2024-12-31"
    assert parent.data.params["arg.rolling"] == "True"
    names = {a.path for a in c.list_artifacts(rid)}
    assert {"lauf.json", "metrics.csv", "de_lu_v1.yaml"} <= names
    exp = c.get_experiment_by_name("de_lu_v1")
    assert exp is not None
    kids = c.search_runs([exp.experiment_id], f"tags.mlflow.parentRunId = '{rid}'")
    by = {r.data.tags["method"]: r for r in kids}
    assert set(by) == {"kalender", "naive_day"}
    k = by["kalender"].data
    assert k.metrics["load_actual.d1.mae"] == 1001.0
    assert k.metrics["w.load_actual.d2.mae"] == 1002.0
    assert "load_actual.d1.pinball" not in k.metrics  # NaN wird nicht gemeldet
    assert k.tags["compare_complete"] == "true" and k.tags["baseline"] == "false"
    assert by["naive_day"].data.tags["compare_complete"] == "false"


def test_registry_mirror_roles_and_aliases(tmp_path: Path, mlflow_dir: Path) -> None:
    """@relation(SWR-MLT-047, scope=function)"""
    from mlflow import MlflowClient

    cfg, _ = load_config(CFG_PATH)
    reg = Registry(str(tmp_path / "reg"), cfg)
    v1, v2, v3 = (f"de_lu_v1-kalender-20261{i}010000-abc123" for i in range(3))
    for v in (v1, v2, v3):
        man = {"version": v, "snapshot_id": "abc123", "variant": {"name": "kalender"}}
        reg.store.write(f"de_lu_v1/{v}/manifest.json", json.dumps(man).encode())
    st = {"champion": v1, "challengers": [v2], "baseline": v3, "previous": [], "history": []}
    reg._save_state(st)
    assert tracking.mirror_registry(reg) == 3
    c = MlflowClient()
    champ = c.get_model_version_by_alias("de_lu_v1", "champion")
    assert champ.tags["pdb_version"] == v1 and champ.tags["role"] == "champion"
    assert champ.source == f"{reg.store.root}/de_lu_v1/{v1}"
    # Freigabe in der Datei-Registry: v2 wird Champion, v1 Vorgänger
    reg._save_state({**st, "champion": v2, "challengers": [], "previous": [v1]})
    tracking.mirror_registry(reg)
    assert c.get_model_version_by_alias("de_lu_v1", "champion").tags["pdb_version"] == v2
    prev = c.get_model_version_by_alias("de_lu_v1", "previous")
    assert prev.tags["pdb_version"] == v1
    assert c.get_model_version("de_lu_v1", prev.version).tags["role"] == "previous"
    assert len(c.search_model_versions("name = 'de_lu_v1'")) == 3  # keine Doppel


def test_failures_do_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """@relation(SWR-MLT-047, scope=function)"""
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")  # niemand hört zu
    monkeypatch.setenv("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
    monkeypatch.setenv("MLFLOW_HTTP_REQUEST_TIMEOUT", "2")
    cfg, _ = load_config(CFG_PATH)
    out = tmp_path / "bt"
    _backtest_dir(out)
    assert tracking.log_backtest(cfg, CFG_PATH, out, {}) is None
    assert tracking.log_fit(cfg, "v", {"train_rows": 1}, "s3://x/models") is None
