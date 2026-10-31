"""Modell-Registry mit Champion/Challenger (ARCHITEKTUR.md 7.7), als Dateien abgelegt.

Bis MLflow läuft, liegt die Registry als Verzeichnisbaum lokal oder in Garage
(``s3://ml-artifacts/models``)::

    <root>/<konfiguration>/registry.json           Rollen und Verlauf
    <root>/<konfiguration>/<version>/model.json    Modell (LightGBM-Texte daneben)
    <root>/<konfiguration>/<version>/manifest.json Konfig-Hash, Snapshot, Git-Commit, Kennzahlen
    <root>/<konfiguration>/<version>/backtest.parquet  Backtest-Prognosen für den Vergleich

Rollen: ``champion`` (Produktion), ``challengers`` (laufen im Schatten mit), ``baseline``
(LightGBM ohne Wetter als Maßstab), ``previous`` (frühere Champions als Rückfall). Aktiv
inferiert werden Champion, Challenger, Baseline und der letzte Vorgänger.

Ein Challenger wird nur Champion, wenn er auf denselben Backtest-Zeilen im Mittel besser ist
und in keiner Gruppe (Ziel, Vorlauftag und Jahreszeit) mit genug Werten deutlich schlechter.

@relation(SWA-MLT-REGISTRY, scope=file)
"""

from __future__ import annotations

import io
import json
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ml_train.config import ModelConfig, PromotionRule
from ml_train.metrics import add_context, common_rows, score
from ml_train.models import TwoStageModel


class Storage:
    """Minimaler Dateizugriff auf ein lokales Verzeichnis oder ``s3://bucket/prefix``.

    @relation(SWR-MLT-050, scope=class)
    """

    def __init__(self, root: str) -> None:
        self.root = root.rstrip("/")
        self._s3 = None
        self._prefix = ""
        if root.startswith("s3://"):
            from predictiondb_core.s3 import s3_store

            bucket, _, prefix = root[5:].partition("/")
            self._s3 = s3_store(bucket)
            self._prefix = prefix.strip("/")

    def _key(self, rel: str) -> str:
        return f"{self._prefix}/{rel}" if self._prefix else rel

    def read(self, rel: str) -> bytes | None:
        if self._s3 is None:
            p = Path(self.root) / rel
            return p.read_bytes() if p.exists() else None
        import obstore

        try:
            return bytes(obstore.get(self._s3, self._key(rel)).bytes())
        except FileNotFoundError:
            return None

    def write(self, rel: str, data: bytes) -> None:
        if self._s3 is None:
            p = Path(self.root) / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return
        import obstore

        obstore.put(self._s3, self._key(rel), data)

    def upload_dir(self, rel: str, local: Path) -> None:
        for f in sorted(local.iterdir()):
            if f.is_file():
                self.write(f"{rel}/{f.name}", f.read_bytes())

    def download_dir(self, rel: str, names: list[str], local: Path) -> None:
        local.mkdir(parents=True, exist_ok=True)
        for n in names:
            data = self.read(f"{rel}/{n}")
            if data is None:
                raise FileNotFoundError(f"{self.root}/{rel}/{n}")
            (local / n).write_bytes(data)


@dataclass
class Decision:
    promote: bool
    reasons: list[str]
    overall: pd.DataFrame
    groups: pd.DataFrame = field(default_factory=pd.DataFrame)


def compare(champion: pd.DataFrame, challenger: pd.DataFrame, rule: PromotionRule) -> Decision:
    """Vergleicht zwei Backtest-Prognosesätze (Langform) auf ihren gemeinsamen Zeilen.

    @relation(SWR-MLT-051, scope=function)
    """
    a = champion.assign(method="champion")
    b = challenger.assign(method="challenger")
    both = common_rows(pd.concat([a, b], ignore_index=True), ["champion", "challenger"])
    if both.empty:
        return Decision(False, ["keine gemeinsamen Backtest-Zeilen"], pd.DataFrame())
    ctx = add_context(both)
    metric = "pinball" if ctx["q10"].notna().all() else "pinball50"
    overall = score(ctx, ["method", "target"]).pivot(
        index="target", columns="method", values=metric
    )
    overall["gain"] = 1 - overall["challenger"] / overall["champion"]
    groups = score(ctx, ["method", "target", "lead_day", "season"])
    gp = groups.pivot_table(
        index=["target", "lead_day", "season"], columns="method", values=[metric, "n"]
    )
    g = pd.DataFrame(
        {
            "champion": gp[(metric, "champion")],
            "challenger": gp[(metric, "challenger")],
            "n": gp[("n", "challenger")],
        }
    )
    g["loss"] = g["challenger"] / g["champion"] - 1
    reasons = []
    for target, row in overall.iterrows():
        if row["gain"] < rule.min_gain:
            reasons.append(f"{target}: im Mittel nicht besser ({row['gain']:+.1%})")
    bad = g[(g["n"] >= rule.min_group_n) & (g["loss"] > rule.max_group_loss)]
    for (target, lead, season), row in bad.iterrows():
        reasons.append(f"{target} Tag {lead} {season}: {row['loss']:+.1%} schlechter")
    return Decision(not reasons, reasons, overall.reset_index(), g.reset_index())


class Registry:
    def __init__(self, root: str, cfg: ModelConfig) -> None:
        self.store = Storage(root)
        self.cfg = cfg
        self.base = cfg.name

    # Zustand --------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        raw = self.store.read(f"{self.base}/registry.json")
        if raw is None:
            return {
                "champion": None,
                "challengers": [],
                "baseline": None,
                "previous": [],
                "history": [],
            }
        return dict(json.loads(raw))

    def _save_state(self, st: dict[str, Any]) -> None:
        self.store.write(f"{self.base}/registry.json", json.dumps(st, indent=2).encode())

    def _log(self, st: dict[str, Any], action: str, version: str, note: str = "") -> None:
        st["history"].append(
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "action": action,
                "version": version,
                "note": note,
            }
        )

    def active(self) -> list[str]:
        """@relation(SWR-MLT-060, scope=function)"""
        st = self.state()
        out = [st["champion"], *st["challengers"], st["baseline"], *st["previous"][:1]]
        return [v for v in dict.fromkeys(out) if v]

    def manifest(self, version: str) -> dict[str, Any]:
        raw = self.store.read(f"{self.base}/{version}/manifest.json")
        if raw is None:
            raise KeyError(f"Version {version} fehlt in der Registry")
        return dict(json.loads(raw))

    def attach_backtest(
        self, version: str, backtest: pd.DataFrame, extra: dict[str, Any] | None = None
    ) -> None:
        """Backtest-Prognosen nachträglich zu einer registrierten Version legen.

        Für Versionen, die ohne ``--backtest`` trainiert wurden. Der Backtest muss mit derselben
        Konfiguration und Variante gerechnet sein; der Registry-Spiegel liest daraus beim
        nächsten Abgleich die Kennzahlen.

        @relation(SWR-MLT-052, scope=function)
        """
        man = self.manifest(version)
        buf = io.BytesIO()
        backtest.to_parquet(buf, index=False)
        self.store.write(f"{self.base}/{version}/backtest.parquet", buf.getvalue())
        files = list(man.get("files", []))
        if "backtest.parquet" not in files:
            files.insert(
                len(files) - 1 if "manifest.json" in files else len(files), "backtest.parquet"
            )
        man = {**man, **(extra or {}), "files": files}
        self.store.write(
            f"{self.base}/{version}/manifest.json",
            json.dumps(man, indent=2, default=str).encode(),
        )

    # Ablegen und Laden ------------------------------------------------------------------------

    def register(
        self,
        model: TwoStageModel,
        manifest: dict[str, Any],
        backtest: pd.DataFrame | None,
        role: str = "challenger",
    ) -> str:
        """@relation(SWR-MLT-050, scope=function)"""
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M")
        version = f"{self.cfg.name}-{model.variant.name}-{stamp}-{manifest['snapshot_id'][:6]}"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            spec = model.save(d)
            files = sorted(p.name for p in d.iterdir())
            if backtest is not None:
                backtest.to_parquet(d / "backtest.parquet", index=False)
                files.append("backtest.parquet")
            man = {
                **manifest,
                "version": version,
                "files": [*files, "manifest.json"],
                "variant": spec["variant"],
            }
            (d / "manifest.json").write_text(json.dumps(man, indent=2, default=str))
            self.store.upload_dir(f"{self.base}/{version}", d)
        st = self.state()
        if role == "champion":
            self._make_champion(st, version, "direkt als Champion registriert")
        elif role == "baseline":
            st["baseline"] = version
            self._log(st, "baseline", version)
        else:
            st["challengers"] = [*st["challengers"], version]
            self._log(st, "challenger", version)
        self._save_state(st)
        return version

    def _make_champion(self, st: dict[str, Any], version: str, note: str) -> None:
        if st["champion"] and st["champion"] != version:
            st["previous"] = [st["champion"], *st["previous"]]
        st["champion"] = version
        st["challengers"] = [v for v in st["challengers"] if v != version]
        self._log(st, "champion", version, note)

    def load(self, version: str, cache: Path) -> TwoStageModel:
        man = self.manifest(version)
        names = [f for f in man["files"] if f.endswith((".txt", ".json"))]
        local = cache / version
        self.store.download_dir(f"{self.base}/{version}", names, local)
        return TwoStageModel.load(self.cfg, local)

    def backtest(self, version: str) -> pd.DataFrame | None:
        raw = self.store.read(f"{self.base}/{version}/backtest.parquet")
        return None if raw is None else pd.read_parquet(io.BytesIO(raw))

    # Champion/Challenger --------------------------------------------------------------------

    def evaluate(self, challenger: str) -> Decision:
        """@relation(SWR-MLT-051, scope=function)"""
        st = self.state()
        if not st["champion"]:
            return Decision(True, [], pd.DataFrame())
        a, b = self.backtest(st["champion"]), self.backtest(challenger)
        if a is None or b is None:
            return Decision(False, ["Backtest-Prognosen fehlen"], pd.DataFrame())
        return compare(a, b, self.cfg.promotion)

    def promote(self, challenger: str, force: bool = False) -> Decision:
        """@relation(SWR-MLT-051, scope=function)"""
        d = self.evaluate(challenger)
        if d.promote or force:
            st = self.state()
            self._make_champion(st, challenger, "erzwungen" if force and not d.promote else "")
            self._save_state(st)
        return d
