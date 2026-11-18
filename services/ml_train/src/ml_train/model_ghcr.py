"""Modelle und Registry-Stand in GHCR (ARCHITEKTUR.md 7.11): Actions registriert, die VM importiert.

Ein Repository je Markt, ``ghcr.io/the78mole/predictiondb-models/de_lu``. Darin liegen:

- **Versionen:** Tag = Versionsname der Registry (``de_lu_v2-frei-202611180300-ab12cd``), ein
  Layer je Datei des Versionsordners (Modell, ``manifest.json``, ``backtest.parquet``), dazu
  Kennzahlen als Annotationen. Beim ersten Push als Challenger kommt
  ``challenger-JJJJ-MM-TT-<konfiguration>-<variante>`` dazu.
- **Stand der Registry:** Tag ``history``, ein Layer ``registry-<konfiguration>.json`` je
  Konfiguration (Rollen, Verlauf, Variante je Version). Die VM schreibt ihn nach jeder Änderung
  (``ml-train registry publish-state``), Actions liest daraus die aktiven Varianten.
- **Rollen-Tags:** ``champion`` und ``baseline`` wandern, ``champion-JJJJ-MM-TT`` und
  ``honor-JJJJ-MM-TT`` bleiben, gesetzt nur an Versionen, die in GHCR liegen.
- **Backtest-Bericht:** ``backtest-<konfiguration>`` (wandert) und
  ``backtest-<konfiguration>-JJJJ-MM-TT``: nur Kennzahlen als CSV, ``lauf.json`` und die Rollen
  zur Zeit des Laufs, keine Prognosen. Die Pages-Seite zeigt daraus den Modellvergleich.

Hochgeladen wird nur eine Version, deren Snapshot ausschließlich freie Quellen enthält
(``publishable`` im Manifest der Version, 7.11.1).

@relation(SWA-MLT-MODEL-GHCR, scope=file)
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from ml_train.metrics import add_context, score
from ml_train.registry import Registry as ModelRegistry
from predictiondb_core.log import get_logger

log = get_logger(__name__)

#: Repository der Modelle eines Marktes (7.11.2), über ``PDB_MODELS_REPOSITORY`` änderbar
REPOSITORY = os.environ.get("PDB_MODELS_REPOSITORY", "the78mole/predictiondb-models/de_lu")
ARTIFACT_MODEL = "application/vnd.predictiondb.model.v1"
ARTIFACT_STATE = "application/vnd.predictiondb.model-history.v1"
ARTIFACT_REPORT = "application/vnd.predictiondb.backtest-report.v1"
FILE_MEDIA = "application/vnd.predictiondb.file.v1"
TITLE = "org.opencontainers.image.title"
DESCRIPTION = "org.opencontainers.image.description"
PREFIX = "io.predictiondb."
STATE_TAG = "history"
SOURCE = "https://github.com/the78mole/PredictionDB"
#: GitHub zeigt die Beschreibung bis zu dieser Länge
DESCRIPTION_LIMIT = 512
#: Vorlauftage, deren MAE als Annotation am Modell steht
ANNOTATED_LEADS = (1, 2)
#: Ziele in der Kurzbeschreibung: Code, Bezeichnung, Einheit, Teiler (Last in MW → GW)
SUMMARY = (("da_price_de_lu", "Preis", "€/MWh", 1.0), ("load_actual", "Last", "GW", 1000.0))
#: Kennzahlen-Dateien des Backtests, die in den Bericht kommen (keine Prognosen)
REPORT_FILES = (
    "metrics.csv",
    "metrics_lead.csv",
    "metrics_season.csv",
    "metrics_year.csv",
    "metrics_common_lead.csv",
    "metrics_window.csv",
    "metrics_window_lead.csv",
    "metrics_rolling.csv",
    "metrics_rolling_lead.csv",
    "vergleich_jahreszeit.csv",
    "vergleich_jahreszeit_fenster.csv",
    "vergleich_jahreszeit_rolling.csv",
    "rank_corr.csv",
    "lauf.json",
)
_TAG = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")


class NotPublishable(RuntimeError):
    """Version ohne Nachweis, dass ihr Snapshot nur freie Quellen enthält."""


def connect(client: httpx.Client, *, read_only: bool = False) -> Any:
    """Registry-Client für das Modell-Repository; Anmeldung über ``WX_EXCHANGE_*`` wie Silber."""
    from wx_exchange.oci import Registry

    return Registry(
        os.environ.get("WX_EXCHANGE_REGISTRY", "ghcr.io"),
        REPOSITORY,
        client,
        username=os.environ.get("WX_EXCHANGE_USERNAME"),
        password=os.environ.get("WX_EXCHANGE_TOKEN") or None,
        read_only=read_only,
    )


def check_tag(tag: str) -> str:
    if not _TAG.match(tag):
        raise ValueError(f"kein gültiger OCI-Tag: {tag!r}")
    return tag


def challenger_tag(day: date, config: str, variant: str) -> str:
    """Bleibender Tag beim Einstieg als Challenger (7.11.3), immer mit Konfiguration/Variante.

    @relation(SWR-MLT-080, scope=function)
    """
    return check_tag(f"challenger-{day.isoformat()}-{config}-{variant}")


# --- Annotationen ----------------------------------------------------------------------------


def lead_mae(backtest: pd.DataFrame | None) -> dict[tuple[str, int], float]:
    """MAE je Ziel und Vorlauftag aus den Backtest-Prognosen einer Version."""
    if backtest is None or backtest.empty:
        return {}
    m = score(add_context(backtest), ["target", "lead_day"])
    return {(str(r.target), int(r.lead_day)): float(r.mae) for r in m.itertuples()}


def _fmt(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def model_annotations(man: dict[str, Any], mae: dict[tuple[str, int], float]) -> dict[str, str]:
    """Annotationen am Modell-Manifest (7.11.4): Kurzbeschreibung, Kennzahlen, Herkunft.

    @relation(SWR-MLT-080, scope=function)
    """
    variant = man["variant"]["name"]
    train = man.get("train_issue_days") or ["?", "?"]
    parts = [f"{man['config']}/{variant}"]
    for target, label, unit, scale in SUMMARY:
        vals = [
            f"D+{d} {_fmt(mae[(target, d)] / scale)}" for d in ANNOTATED_LEADS if (target, d) in mae
        ]
        if vals:
            parts.append(f"{label} MAE {', '.join(vals)} {unit}")
    parts.append(f"Training {train[0]} bis {train[1]}")
    parts.append(f"CC BY 4.0, Quellen siehe {PREFIX}sources. Doku: {SOURCE}")
    text = " · ".join(parts)
    out = {
        DESCRIPTION: text[:DESCRIPTION_LIMIT],
        "org.opencontainers.image.licenses": "CC-BY-4.0",
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.revision": str(man.get("git_commit") or ""),
        "org.opencontainers.image.created": str(man.get("created") or ""),
        f"{PREFIX}version": man["version"],
        f"{PREFIX}config": man["config"],
        f"{PREFIX}variant": variant,
        f"{PREFIX}snapshot": str(man.get("snapshot_id") or ""),
        f"{PREFIX}clustering": str(man.get("clustering_version") or ""),
        f"{PREFIX}train.from": str(train[0]),
        f"{PREFIX}train.to": str(train[1]),
        f"{PREFIX}sources": json.dumps(man.get("sources") or {}, sort_keys=True),
    }
    for (target, lead), v in sorted(mae.items()):
        if lead in ANNOTATED_LEADS:
            out[f"{PREFIX}metric.{target}.d{lead}.mae"] = f"{v:.4g}"
    return out


# --- Artefakte -------------------------------------------------------------------------------


def _push(
    oci: Any,
    files: dict[str, bytes],
    artifact_type: str,
    notes: dict[str, str],
    tags: Iterable[str],
) -> str:
    from wx_exchange.oci import EMPTY, MANIFEST, digest

    layers = []
    for name in sorted(files):
        d = oci.push_blob(files[name])
        layers.append(
            {
                "mediaType": FILE_MEDIA,
                "digest": d,
                "size": len(files[name]),
                "annotations": {TITLE: name},
            }
        )
    manifest = {
        "schemaVersion": 2,
        "mediaType": MANIFEST,
        "artifactType": artifact_type,
        "config": EMPTY,
        "layers": layers,
        "annotations": notes,
    }
    out = ""
    for t in dict.fromkeys(tags):
        out = oci.put_manifest(check_tag(t), manifest)
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return out or digest(body)


def _pull(oci: Any, tag: str) -> tuple[dict[str, bytes], dict[str, Any]] | None:
    m = oci.manifest(tag)
    if m is None:
        return None
    files = {}
    for layer in m.get("layers", []):
        name = (layer.get("annotations") or {}).get(TITLE)
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"{tag}: Layer ohne gültigen Dateinamen: {name!r}")
        files[name] = oci.blob(layer["digest"])
    return files, m


def retag(oci: Any, source: str, tags: Iterable[str]) -> bool:
    """Weitere Tags auf ein vorhandenes Manifest; ``False``, wenn es ``source`` nicht gibt."""
    m = oci.manifest(source)
    if m is None:
        return False
    for t in tags:
        oci.put_manifest(check_tag(t), m)
    return True


def version_files(reg: ModelRegistry, version: str) -> dict[str, bytes]:
    man = reg.manifest(version)
    out = {}
    for name in man["files"]:
        data = reg.store.read(f"{reg.base}/{version}/{name}")
        if data is None:
            raise FileNotFoundError(f"{version}/{name} fehlt in der Registry")
        out[name] = data
    return out


def push_version(oci: Any, reg: ModelRegistry, version: str, extra_tags: Iterable[str] = ()) -> str:
    """Eine Version der Registry nach GHCR (Tag = Versionsname, dazu ``extra_tags``).

    @relation(SWR-MLT-080, scope=function)
    """
    man = reg.manifest(version)
    if man.get("publishable") is not True:
        raise NotPublishable(
            f"{version}: Snapshot nicht als frei ausgewiesen (publishable fehlt oder false)"
        )
    files = version_files(reg, version)
    notes = model_annotations(man, lead_mae(reg.backtest(version)))
    d = _push(oci, files, ARTIFACT_MODEL, notes, [version, *extra_tags])
    log.info("model.pushed", version=version, digest=d, tags=list(extra_tags))
    return d


def pull_version(oci: Any, tag: str, dest: Path) -> dict[str, Any]:
    """Version aus GHCR in einen Ordner; gibt ihr ``manifest.json`` zurück."""
    got = _pull(oci, tag)
    if got is None:
        raise KeyError(f"{tag} fehlt in GHCR")
    files, _ = got
    if "manifest.json" not in files:
        raise ValueError(f"{tag}: manifest.json fehlt im Artefakt")
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (dest / name).write_bytes(data)
    return dict(json.loads(files["manifest.json"]))


def remote_versions(oci: Any, config: str) -> list[str]:
    """Versionen einer Konfiguration in GHCR (Tags ``<konfiguration>-…``), sortiert."""
    return sorted(t for t in oci.tags() if t.startswith(f"{config}-"))


@dataclass
class ImportResult:
    imported: list[str]
    present: list[str]


def import_versions(
    oci: Any, reg: ModelRegistry, versions: Iterable[str] | None = None
) -> ImportResult:
    """Versionen aus GHCR, die der Registry fehlen, als Challenger übernehmen (VM).

    @relation(SWR-MLT-081, scope=function)
    """
    wanted = list(versions) if versions is not None else remote_versions(oci, reg.base)
    done, present = [], []
    for v in wanted:
        if reg.has(v):
            present.append(v)
            continue
        with tempfile.TemporaryDirectory() as tmp:
            man = pull_version(oci, v, Path(tmp))
            if man.get("config") != reg.base or man.get("version") != v:
                other = f"{man.get('config')}/{man.get('version')}"
                raise ValueError(f"{v}: Artefakt gehört zu {other}")
            reg.import_version(Path(tmp), note="aus GHCR")
        done.append(v)
        log.info("model.imported", version=v)
    return ImportResult(done, present)


# --- Stand der Registry ----------------------------------------------------------------------


def state_payload(reg: ModelRegistry) -> dict[str, Any]:
    """Rollen, Verlauf und Variante je bekannter Version (für Actions und die Pages-Seite)."""
    st = reg.state()
    known = {st["champion"], st["baseline"], *st["challengers"], *st["previous"]}
    known |= {h["version"] for h in st["history"]}
    variants = {}
    for v in sorted(x for x in known if x):
        try:
            variants[v] = reg.manifest(v)["variant"]["name"]
        except KeyError:
            continue
    return {
        "config": reg.base,
        "updated": datetime.now(UTC).isoformat(timespec="seconds"),
        **st,
        "variants": variants,
    }


def read_states(oci: Any) -> dict[str, dict[str, Any]]:
    """Alle Registry-Stände aus ``:history``, je Konfiguration; leer, wenn es ihn nicht gibt."""
    got = _pull(oci, STATE_TAG)
    if got is None:
        return {}
    out = {}
    for name, data in got[0].items():
        m = re.fullmatch(r"registry-(.+)\.json", name)
        if m:
            out[m[1]] = dict(json.loads(data))
    return out


def role_tags(state: dict[str, Any]) -> dict[str, str]:
    """Tag → Version aus dem Verlauf (7.11.3): Krönungen, Ehrenplätze, wandernde Rollen.

    @relation(SWR-MLT-082, scope=function)
    """
    tags: dict[str, str] = {}
    champion: str | None = None
    for h in state.get("history", []):
        day = str(h["at"])[:10]
        if h["action"] == "champion":
            if champion and champion != h["version"]:
                tags[f"honor-{day}"] = champion
            champion = h["version"]
            tags[f"champion-{day}"] = champion
    if state.get("champion"):
        tags["champion"] = state["champion"]
    if state.get("baseline"):
        tags["baseline"] = state["baseline"]
    return tags


def publish_state(oci: Any, reg: ModelRegistry) -> dict[str, Any]:
    """Stand der Registry nach ``:history`` und Rollen-Tags an Versionen in GHCR setzen (VM).

    Andere Konfigurationen im selben Repository bleiben unverändert. Tags zeigen nur auf
    Versionen, die in GHCR liegen; die übrigen stehen in ``skipped``.

    @relation(SWR-MLT-082, scope=function)
    """
    payload = state_payload(reg)
    got = _pull(oci, STATE_TAG)
    files = dict(got[0]) if got else {}
    files[f"registry-{reg.base}.json"] = json.dumps(payload, indent=1, sort_keys=True).encode()
    notes = {
        DESCRIPTION: "Stand der Modell-Registry je Konfiguration (Rollen, Verlauf). "
        f"Doku: {SOURCE}",
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.created": payload["updated"],
    }
    _push(oci, files, ARTIFACT_STATE, notes, [STATE_TAG])
    remote = set(oci.tags())
    set_, skipped = {}, {}
    for tag, version in role_tags(payload).items():
        if version in remote:
            retag(oci, version, [tag])
            set_[tag] = version
        else:
            skipped[tag] = version
    log.info("model.state", config=reg.base, tags=set_, skipped=skipped)
    return {"tags": set_, "skipped": skipped}


def active_variants(state: dict[str, Any], cfg_variants: list[str]) -> list[str]:
    """Varianten der aktiven Versionen (wie ``Registry.active``) in der Reihenfolge der Konfig.

    @relation(SWR-MLT-046, scope=function)
    """
    active = [state.get("champion"), *state.get("challengers", []), state.get("baseline")]
    active += state.get("previous", [])[:1]
    used = {state.get("variants", {}).get(v) for v in active if v}
    return [v for v in cfg_variants if v in used]


def roles_by_variant(state: dict[str, Any]) -> dict[str, list[str]]:
    """Variante → Rollen (``champion``, ``challenger``, ``vorgaenger``, ``baseline``)."""
    var = state.get("variants", {})
    out: dict[str, list[str]] = {}

    def add(version: str | None, role: str) -> None:
        if version and var.get(version):
            out.setdefault(var[version], [])
            if role not in out[var[version]]:
                out[var[version]].append(role)

    add(state.get("champion"), "champion")
    for v in state.get("challengers", []):
        add(v, "challenger")
    for v in state.get("previous", [])[:1]:
        add(v, "vorgaenger")
    add(state.get("baseline"), "baseline")
    return out


# --- Backtest-Bericht ------------------------------------------------------------------------


def report_tags(config: str, day: date) -> list[str]:
    return [check_tag(f"backtest-{config}"), check_tag(f"backtest-{config}-{day.isoformat()}")]


def push_report(
    oci: Any, folder: Path, config: str, state: dict[str, Any] | None, day: date | None = None
) -> list[str]:
    """Kennzahlen eines Backtests als Bericht nach GHCR, mit den Rollen zur Zeit des Laufs.

    @relation(SWR-MLT-083, scope=function)
    """
    files = {n: (folder / n).read_bytes() for n in REPORT_FILES if (folder / n).exists()}
    if "metrics.csv" not in files:
        raise FileNotFoundError(f"{folder}: metrics.csv fehlt, kein Backtest-Ergebnis")
    roles = {"config": config, "roles": roles_by_variant(state or {}), "state": state or {}}
    files["rollen.json"] = json.dumps(roles, indent=1, sort_keys=True).encode()
    run = json.loads(files.get("lauf.json", b"{}"))
    notes = {
        DESCRIPTION: f"Backtest {config}, Kennzahlen je Variante und Vergleichsverfahren "
        f"(keine Prognosen). Doku: {SOURCE}",
        "org.opencontainers.image.source": SOURCE,
        "org.opencontainers.image.licenses": "CC-BY-4.0",
        "org.opencontainers.image.revision": str(run.get("git_commit") or ""),
        "org.opencontainers.image.created": str(run.get("finished") or ""),
        f"{PREFIX}config": config,
        f"{PREFIX}snapshot": str(run.get("snapshot_id") or ""),
    }
    tags = report_tags(config, day or datetime.now(UTC).date())
    _push(oci, files, ARTIFACT_REPORT, notes, tags)
    return tags


def pull_report(oci: Any, config: str) -> dict[str, bytes] | None:
    got = _pull(oci, f"backtest-{config}")
    return None if got is None else got[0]


def read_csv(files: dict[str, bytes], name: str) -> pd.DataFrame | None:
    return pd.read_csv(io.BytesIO(files[name])) if name in files else None
