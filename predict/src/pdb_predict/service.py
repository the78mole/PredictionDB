"""Ein Prognoselauf: Paket holen, Eingänge aktualisieren, Merkmale bilden, rechnen, ausgeben.

Ausgabe in ``out/`` (Begründung im README):

- ``latest.json``: alle Ziele mit P10/P50/P90 je Stunde, Modell, Quellen und Lizenzvermerk
- ``latest.csv``: dieselben Werte als Tabelle ``target, valid_time, p10, p50, p90``
- ``archive/<JJJJMMTTThhZ>.json``: jede Ausgabe, ``keep_days`` Tage lang

Dateien werden über eine temporäre Datei ersetzt, damit Leser nie eine halbe Datei sehen.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from energy_ingest.model import SERIES
from ml_train.dataset import FeatureBuilder
from ml_train.predict import predict_model
from ml_train.snapshot import Snapshot
from pdb_predict.energy import EnergyCache, Window
from pdb_predict.package import ModelPackage
from pdb_predict.weather import WeatherCache
from predictiondb_core.log import get_logger

log = get_logger(__name__)

SCHEMA = "predictiondb.forecast.v1"


def needs(pkg: ModelPackage, issue: datetime) -> dict[str, Window]:
    """Zeitfenster je Reihe: Lags 45 Tage, Reihen der ``asof``-Gruppen so weit wie nötig."""
    cfg = pkg.cfg
    end = issue + timedelta(days=max(cfg.issue.lead_days) + 2)
    lag_start = issue - timedelta(days=45)
    asof = {
        c
        for name in pkg.model.variant.groups
        if (g := cfg.series_groups.get(name)) is not None and g.mode == "asof"
        for c in g.codes
    }
    long_start = issue - timedelta(days=int(pkg.info.get("lookback_days", 45)))
    return {c: Window(long_start if c in asof else lag_start, end) for c in pkg.codes}


def unit(code: str) -> str | None:
    s = SERIES.get(code)
    return s.unit if s else None


def to_document(
    df: pd.DataFrame,
    pkg: ModelPackage,
    issue: datetime,
    sources: dict[str, str],
    missing: dict[str, str],
    weather_run: datetime | None,
) -> dict[str, Any]:
    targets: dict[str, list[dict[str, Any]]] = {}
    if not df.empty:
        wide = df.pivot_table(
            index=["target", "valid_time"], columns="quantile", values="value"
        ).sort_index()
        for (target, valid), row in wide.iterrows():
            targets.setdefault(str(target), []).append(
                {
                    "valid_time": pd.Timestamp(valid).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    **{f"p{round(q * 100)}": round(float(v), 3) for q, v in row.items()},
                }
            )
    attribution = "; ".join([str(pkg.info.get("attribution", "")), *sorted(set(sources.values()))])
    return {
        "schema": SCHEMA,
        "issue_time": issue.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": pkg.describe(),
        "weather_run": weather_run.isoformat() if weather_run else None,
        "units": {t: unit(t) for t in targets},
        "targets": targets,
        "inputs_missing": missing,
        "sources": [{"code": k, "license": v} for k, v in sorted(sources.items())],
        "license": "CC BY 4.0 (Modell und Prognose)",
        "attribution": attribution,
    }


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_outputs(doc: dict[str, Any], out: Path, keep_days: int) -> Path:
    body = json.dumps(doc, indent=1, ensure_ascii=False).encode()
    stamp = doc["issue_time"].replace("-", "").replace(":", "")[:12] + "Z"
    archive = out / "archive" / f"{stamp}.json"
    _write(archive, body)
    _write(out / "latest.json", body)
    _write(out / "latest.csv", to_csv(doc).encode())
    cutoff = datetime.now(UTC) - timedelta(days=keep_days)
    for f in (out / "archive").glob("*.json"):
        if datetime.fromtimestamp(f.stat().st_mtime, UTC) < cutoff:
            f.unlink()
    return archive


def to_csv(doc: dict[str, Any], target: str | None = None) -> str:
    lines = ["target,valid_time,p10,p50,p90"]
    for t, rows in doc["targets"].items():
        if target and t != target:
            continue
        for r in rows:
            lines.append(
                f"{t},{r['valid_time']},{r.get('p10', '')},{r.get('p50', '')},{r.get('p90', '')}"
            )
    return "\n".join(lines) + "\n"


class Predictor:
    def __init__(
        self,
        package: Callable[[], ModelPackage],
        energy: EnergyCache,
        weather: Callable[[ModelPackage], WeatherCache] | None = None,
    ) -> None:
        self.package = package
        self.energy = energy
        self.weather_factory = weather
        self._weather: WeatherCache | None = None

    def run(self, issue: datetime | None = None) -> dict[str, Any]:
        issue = issue or datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        pkg = self.package()
        need = needs(pkg, issue)
        missing = self.energy.update(need, now=datetime.now(UTC))
        energy = self.energy.snapshot_energy(need)
        weather = pd.DataFrame(columns=["run_time", "valid_time", "feature", "value"])
        if pkg.requires_weather:
            if self._weather is None and self.weather_factory is not None:
                self._weather = self.weather_factory(pkg)
            if self._weather is not None:
                weather = self._weather.update(pkg)
        snap = Snapshot(energy, weather, pkg.capacity, {"snapshot_id": "live"})
        frame = FeatureBuilder(snap, pkg.cfg).build_at([pd.Timestamp(issue)])
        df = predict_model(frame, pkg.model, pkg.version, pd.Timestamp(issue))
        run = None
        if pkg.requires_weather and "wx_lead_h" in frame.X.columns and not df.empty:
            lead = frame.X["wx_lead_h"].dropna()
            ts = frame.meta.loc[lead.index, "ts"]
            if len(lead):
                run = (ts.iloc[0] - pd.Timedelta(hours=float(lead.iloc[0]))).to_pydatetime()
        sources = self.energy.sources_used(need)
        # Bestand (und Cluster) im Paket stammen aus dem MaStR
        sources["mastr"] = "Marktstammdatenregister, Datenlizenz Deutschland - Namensnennung 2.0"
        if pkg.requires_weather:
            sources["noaa_gfs"] = "NOAA GFS, gemeinfrei (Werk der US-Regierung)"
        doc = to_document(df, pkg, issue, sources, missing, run)
        log.info(
            "predict.done",
            issue=doc["issue_time"],
            version=pkg.version,
            rows=len(df),
            missing=sorted(missing),
        )
        return doc
