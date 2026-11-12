"""Energiedaten im Runner: freie Quellen ohne Datenbank abrufen und je Jahr nach GHCR.

Grundlage für Training und Backtest in GitHub Actions: Der Trainings-Snapshot liest dieselben
Reihen wie ``energy.value_best`` auf der VM, nur aus GHCR statt aus der Datenbank.

Je Kalenderjahr (Ortszeit Berlin, wie die Monats- und Jahresmarktwerte) ruft ``build_year`` alle
Datensätze der freien Quellen mit den Abrufen von ``energy_ingest`` ab und legt ein Artefakt
``predictiondb-data/energy:JJJJ`` an:

- ein Layer je Quelle, ``<quelle>.parquet`` mit ``code``, ``ts`` (UTC), ``value``, ``published``
  (wie ``energy.value``: der reguläre Veröffentlichungstermin, beim Backfill also
  ``estimate_published``),
- ``sources.json`` mit Rang (``priority``), Lizenzklasse, Lizenz und den Reihen je Quelle
  (Art, Einheit, Auflösung).

``value_best`` wählt daraus wie die Datenbank-Sicht je Reihe und Zeitpunkt den Wert der Quelle mit
dem kleinsten Rang. Die eigenen Marktwerte (``pdb_marktwert``) entstehen wie auf der VM aus den
Stundenmitteln von Preis und Erzeugung.

Nur Quellen mit ``free = True`` kommen ins Artefakt; unfreie Quellen und „nur Training“
(Netztransparenz, regelleistung.net) bleiben draußen, weil das Paket öffentlich ist.

@relation(SWA-WXX-ENERGY, scope=file)
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import Engine

from energy_ingest.cli import build_sources
from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.ingest import chunks
from energy_ingest.model import BERLIN, SeriesData, next_period, published_at
from energy_ingest.sources import Source
from energy_ingest.sources.marktwert import PRICE, WEIGHTS, Marktwert, market_values, period_starts
from predictiondb_core.log import get_logger
from wx_exchange.artifact import LICENSE, PREFIX, description
from wx_exchange.oci import EMPTY, MANIFEST, Registry, digest
from wx_exchange.safe import ArtifactError, annotations_of, check_digest, json_object
from wx_exchange.safe import layers as manifest_layers

log = get_logger(__name__)

ARTIFACT_TYPE = "application/vnd.predictiondb.energy.v1"
VALUES_TYPE = "application/vnd.predictiondb.energy-values.v1.parquet"
SOURCES_TYPE = "application/vnd.predictiondb.energy-sources.v1+json"
REPOSITORY = "the78mole/predictiondb-data/energy"
#: Version des Formats und der Auswahl; eine neue Version macht alle Jahre neu fällig
ENERGY_VERSION = "1"
#: Freie Quellen ohne Schlüssel, in der Reihenfolge des Abgleichs auf der VM
SOURCES = [
    "smard",
    "energy_charts",
    "eia",
    "eia_steo",
    "ecb",
    "worldbank",
    "oil_bulletin",
    "eurostat",
    "hubeau",
]
#: Freie Quellen, die nur mit Schlüssel laufen (PDB_ENTSOE_TOKEN, PDB_GIE_API_KEY)
KEYED_SOURCES = ["entsoe_last", "gie_agsi", "gie_alsi"]
MARKTWERT = "pdb_marktwert"
FIRST_YEAR = 2015
#: Laufendes Jahr: Ist- und Prognosewerte nur so weit zurück neu abrufen (Korrekturen der
#: Quellen kommen bis etwa 30 Tage später, wie ``energy-ingest revise``)
INCREMENTAL_DAYS = 62
#: Datensätze mit Blöcken bis zu dieser Größe werden im laufenden Jahr nur ab dem Rückblick
#: abgerufen; größere Blöcke sind ganze Dateien (Monatswerte, Prognosen) und kommen vollständig
INCREMENTAL_CHUNK = timedelta(days=31)
ATTRIBUTION = (
    "Bundesnetzagentur | SMARD.de (CC BY 4.0); Energy-Charts, Fraunhofer ISE (CC BY 4.0);"
    " U.S. Energy Information Administration (Public Domain); Europäische Zentralbank;"
    " Weltbank (CC BY 4.0); Europäische Kommission, Weekly Oil Bulletin (CC BY 4.0);"
    " Eurostat (CC BY 4.0); Hub'eau / eaufrance (Licence Ouverte 2.0); Marktwerte selbst"
    " gerechnet; zusammengestellt von PredictionDB (the78mole). Lizenz je Quelle in sources.json"
)
COLUMNS = ("code", "ts", "value", "published")
SCHEMA = pa.schema(
    [
        ("code", pa.dictionary(pa.int16(), pa.string())),
        ("ts", pa.timestamp("us", tz="UTC")),
        ("value", pa.float64()),
        ("published", pa.timestamp("us", tz="UTC")),
    ]
)
_SOURCE = re.compile(r"\A[a-z][a-z0-9_]{0,31}\Z")


def year_bounds(year: int) -> tuple[datetime, datetime]:
    """Kalenderjahr in Ortszeit Berlin, als UTC (Grenzen der Jahresmarktwerte)."""
    start = datetime(year, 1, 1, tzinfo=BERLIN).astimezone(UTC)
    return start, next_period(start, 12)


def year_of(ts: datetime) -> int:
    return ts.astimezone(BERLIN).year


@dataclass
class SourceLayer:
    """Werte und Reihen einer Quelle in einem Jahr."""

    code: str
    priority: int
    free: bool
    train: bool
    license: str
    rows: pd.DataFrame
    series: dict[str, dict[str, Any]] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)

    def meta(self) -> dict[str, Any]:
        return {
            "priority": self.priority,
            "free": self.free,
            "train": self.train,
            "license": self.license,
            "series": self.series,
            "status": self.status,
        }

    def parquet(self) -> bytes:
        return values_parquet(self.rows)


def empty_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": pd.Series([], dtype=str),
            "ts": pd.Series([], dtype="datetime64[us, UTC]"),
            "value": pd.Series([], dtype="float64"),
            "published": pd.Series([], dtype="datetime64[us, UTC]"),
        }
    )


def values_parquet(rows: pd.DataFrame) -> bytes:
    """Reproduzierbares Parquet (sortiert, zstd), damit gleiche Werte gleiche Digests ergeben.

    @relation(SWR-WXX-080, scope=function)
    """
    df = rows.sort_values(["code", "ts"], kind="stable").reset_index(drop=True)
    table = pa.Table.from_pandas(df[list(COLUMNS)], schema=SCHEMA, preserve_index=False)
    buf = io.BytesIO()
    pq.write_table(
        table,
        buf,
        compression="zstd",
        compression_level=9,
        use_dictionary=["code"],
        write_statistics=True,
    )
    return buf.getvalue()


def read_values(data: bytes) -> pd.DataFrame:
    table = pq.read_table(io.BytesIO(data))
    if not set(COLUMNS) <= set(table.column_names):
        raise ArtifactError(
            f"Energie-Layer: Spalten fehlen: {set(COLUMNS) - set(table.column_names)}"
        )
    df = table.select(list(COLUMNS)).to_pandas()
    df["code"] = df["code"].astype(str)
    return df


def to_rows(
    batch: Iterable[SeriesData], start: datetime, end: datetime, now: datetime
) -> pd.DataFrame:
    """Punkte in ``[start, end)`` als Zeilen; ``published`` wie beim Schreiben in die Datenbank.

    @relation(SWR-WXX-080, scope=function)
    """
    codes: list[str] = []
    ts: list[datetime] = []
    values: list[float] = []
    published: list[datetime] = []
    for data in batch:
        for t, v in data.points:
            if not (start <= t < end) or v is None or v != v:
                continue
            codes.append(data.code)
            ts.append(t)
            values.append(float(v))
            published.append(published_at(data.info.kind, t, data.resolution, now))
    if not codes:
        return empty_rows()
    df = pd.DataFrame({"code": codes, "ts": ts, "value": values, "published": published})
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.as_unit("us")
    df["published"] = pd.to_datetime(df["published"], utc=True).dt.as_unit("us")
    # überlappende Blöcke: der zuletzt abgerufene Wert gilt, wie beim Überschreiben in der DB
    return df.drop_duplicates(["code", "ts"], keep="last").reset_index(drop=True)


def _series_meta(batch: Iterable[SeriesData]) -> dict[str, dict[str, Any]]:
    return {
        d.code: {
            "kind": str(d.info.kind.value),
            "unit": d.info.unit,
            "resolution_s": int(d.resolution.total_seconds()),
        }
        for d in batch
    }


def fetch_source(
    source: Source,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    since: datetime | None = None,
    old: pd.DataFrame | None = None,
) -> SourceLayer:
    """Alle Datensätze einer Quelle für ``[start, end)`` in Blöcken wie im Backfill.

    Mit ``since`` und den bisherigen Werten ``old`` werden Datensätze mit kurzen Blöcken (Ist-
    und Prognosewerte, bis ``INCREMENTAL_CHUNK``) erst ab ``since`` abgerufen und mit den
    bisherigen Werten zusammengeführt (neu gewinnt); ganze Dateien kommen immer vollständig.

    @relation(SWR-WXX-080, SWR-WXX-083, scope=function)
    """
    if not source.free:
        raise ValueError(f"{source.code}: nicht frei, gehört nicht in ein öffentliches Paket")
    parts: list[pd.DataFrame] = []
    series: dict[str, dict[str, Any]] = {}
    status: dict[str, str] = {}
    for ds in source.datasets():
        n = 0
        size = source.max_chunk(ds)
        a0 = max(start, since) if since is not None and size <= INCREMENTAL_CHUNK else start
        try:
            for a, b in chunks(size, a0, end):
                batch = source.fetch(ds, max(a, a0), min(b, end))
                series.update(_series_meta(batch))
                rows = to_rows(batch, start, end, now)
                n += len(rows)
                parts.append(rows)
        except Exception as exc:
            status[ds] = f"failed: {type(exc).__name__}: {str(exc)[:200]}"
            log.warning("energy.dataset_failed", source=source.code, dataset=ds, error=str(exc))
            continue
        status[ds] = "ok"
        log.info("energy.dataset", source=source.code, dataset=ds, values=n)
    if old is not None and not old.empty:
        parts.insert(0, old)
    rows = pd.concat(parts, ignore_index=True) if parts else empty_rows()
    rows = rows.drop_duplicates(["code", "ts"], keep="last").reset_index(drop=True)
    return SourceLayer(
        source.code,
        source.priority,
        source.free,
        source.train,
        source.license,
        rows,
        series,
        status,
    )


def value_best(layers: Iterable[tuple[int, pd.DataFrame]]) -> pd.DataFrame:
    """Je Reihe und Zeitpunkt der Wert der Quelle mit dem kleinsten Rang (``energy.value_best``).

    @relation(SWR-WXX-081, scope=function)
    """
    parts = [df.assign(_rank=prio) for prio, df in layers if not df.empty]
    if not parts:
        return empty_rows()
    df = pd.concat(parts, ignore_index=True)
    df = df.sort_values(["code", "ts", "_rank"], kind="stable")
    df = df.drop_duplicates(["code", "ts"], keep="first")
    return df.drop(columns="_rank").reset_index(drop=True)


def hourly(rows: pd.DataFrame, codes: Iterable[str]) -> list[tuple[datetime, str, float]]:
    """Stundenmittel wie ``time_bucket('1 hour', ts)`` in der Datenbank."""
    sel = rows[rows["code"].isin(list(codes))]
    if sel.empty:
        return []
    hour = sel["ts"].dt.floor("h")
    means = sel.groupby([hour, sel["code"]])["value"].mean()
    return [(t.to_pydatetime(), str(c), float(v)) for (t, c), v in means.items()]


def marktwert_layer(
    best: pd.DataFrame, start: datetime, end: datetime, now: datetime
) -> SourceLayer:
    """Eigene Monats- und Jahresmarktwerte aus Preis und Erzeugung (wie ``Marktwert.fetch``).

    @relation(SWR-WXX-080, scope=function)
    """
    rows = hourly(best, [PRICE, *WEIGHTS])
    batch: list[SeriesData] = []
    for months in (1, 12):
        periods = [p for p in period_starts(start, end, months) if next_period(p, months) <= now]
        batch.extend(market_values(rows, periods, months))
    return SourceLayer(
        MARKTWERT,
        Marktwert.priority,
        Marktwert.free,
        Marktwert.train,
        Marktwert.license,
        to_rows(batch, start, end, now),
        _series_meta(batch),
        {"market_value_month": "ok", "market_value_year": "ok"},
    )


def build_sources_free(codes: list[str], http: Http, settings: EnergySettings) -> list[Source]:
    """Quellen wie ``energy-ingest``, ohne Datenbank; Marktwerte rechnet ``marktwert_layer``."""
    wanted = [c for c in codes if c != MARKTWERT]
    sources = build_sources(wanted, settings, http, cast(Engine, None))
    return [s for s in sources if s.free]


@dataclass
class YearResult:
    year: int
    status: str  # published | unchanged | failed
    tag: str
    rows: dict[str, int]
    failed: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "status": self.status,
            "tag": self.tag,
            "rows": self.rows,
            "failed": self.failed,
        }


def tag(year: int) -> str:
    return f"{year:04d}"


def _descriptor(media: str, data: bytes, title: str, d: str | None = None) -> dict[str, Any]:
    return {
        "mediaType": media,
        "digest": d or digest(data),
        "size": len(data),
        "annotations": {"org.opencontainers.image.title": title},
    }


def _sources_json(layers: list[SourceLayer]) -> bytes:
    meta = {s.code: s.meta() for s in sorted(layers, key=lambda s: s.code)}
    return json.dumps(
        {"version": ENERGY_VERSION, "sources": meta}, sort_keys=True, indent=1
    ).encode()


def _old_layers(registry: Registry, manifest: dict[str, Any] | None) -> dict[str, bytes]:
    """Vorhandene Werte je Quelle (Titel ``<quelle>.parquet``) aus dem bisherigen Artefakt."""
    if manifest is None:
        return {}
    out: dict[str, bytes] = {}
    for d in manifest_layers(manifest):
        title = annotations_of(d).get("org.opencontainers.image.title", "")
        if d.get("mediaType") == VALUES_TYPE and title.endswith(".parquet"):
            out[title[: -len(".parquet")]] = registry.blob(check_digest(d.get("digest")))
    return out


def old_sources(registry: Registry, manifest: dict[str, Any] | None) -> dict[str, Any]:
    if manifest is None:
        return {}
    for d in manifest_layers(manifest):
        if d.get("mediaType") == SOURCES_TYPE:
            body = json_object(registry.blob(check_digest(d.get("digest"))))
            sources = body.get("sources")
            return sources if isinstance(sources, dict) else {}
    return {}


class EnergyBuilder:
    """Ein Jahr abrufen und als Artefakt ablegen; unverändert = kein neues Manifest.

    Scheitert ein Datensatz, bleiben die bisherigen Werte dieser Quelle im Artefakt (Annotation
    ``stale``); gibt es noch keine, wird nichts veröffentlicht.
    """

    def __init__(
        self,
        registry: Registry,
        sources: list[Source],
        *,
        source_url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.registry = registry
        self.sources = sources
        self.source_url = source_url
        self.clock = clock or (lambda: datetime.now(UTC))

    def fetch(
        self,
        year: int,
        *,
        since: datetime | None = None,
        old: dict[str, bytes] | None = None,
        old_meta: dict[str, Any] | None = None,
    ) -> list[SourceLayer]:
        start, end = year_bounds(year)
        now = self.clock()
        end = min(end, now + timedelta(days=14))  # Prognosen reichen etwas in die Zukunft
        layers = []
        for s in self.sources:
            prev = old.get(s.code) if old else None
            frm = since if prev is not None else None
            log.info("energy.source", source=s.code, year=year, since=frm and frm.isoformat())
            prev_rows = read_values(prev) if prev is not None and frm is not None else None
            layers.append(fetch_source(s, start, end, now, since=frm, old=prev_rows))
        # Quellen, die dieser Lauf nicht abruft (``--source``), bleiben wie sie waren
        fetched = {s.code for s in self.sources} | {MARKTWERT}
        for code, data in sorted((old or {}).items()):
            meta = (old_meta or {}).get(code)
            if code in fetched or not isinstance(meta, dict) or not meta.get("free"):
                continue
            layers.append(
                SourceLayer(
                    code,
                    int(meta.get("priority", 100)),
                    bool(meta.get("free")),
                    bool(meta.get("train")),
                    str(meta.get("license", "")),
                    read_values(data),
                    dict(meta.get("series") or {}),
                    {},
                )
            )
        best = value_best((s.priority, s.rows) for s in layers)
        layers.append(marktwert_layer(best, start, end, now))
        return layers

    def build_year(self, year: int, *, full: bool = False) -> YearResult:
        """Ein Jahr abrufen; ohne ``full`` im laufenden Jahr nur den Rückblick neu.

        @relation(SWR-WXX-080, SWR-WXX-082, SWR-WXX-083, scope=function)
        """
        t = tag(year)
        existing = self.registry.manifest(t)
        old = _old_layers(self.registry, existing)
        old_meta = old_sources(self.registry, existing)
        now = self.clock()
        fresh = (
            existing is not None
            and annotations_of(existing).get(PREFIX + "energy_version") == ENERGY_VERSION
        )
        since = None
        if not full and fresh and year == year_of(now):
            since = now - timedelta(days=INCREMENTAL_DAYS)
        layers = self.fetch(year, since=since, old=old, old_meta=old_meta)
        failed: dict[str, str] = {}
        stale: list[str] = []
        blobs: dict[str, bytes] = {}
        for s in layers:
            bad = {ds: st for ds, st in s.status.items() if st != "ok"}
            if bad:
                failed.update({f"{s.code}.{ds}": st for ds, st in bad.items()})
                if s.code not in old:
                    return YearResult(year, "failed", t, {}, failed)
                blobs[s.code] = old[s.code]
                prev = old_meta.get(s.code)
                if isinstance(prev, dict) and isinstance(prev.get("series"), dict):
                    s.series = prev["series"]
                stale.append(s.code)
            else:
                blobs[s.code] = s.parquet()
        sources_json = _sources_json(layers)
        rows = {s.code: len(s.rows) for s in layers}
        descriptors = [
            _descriptor(VALUES_TYPE, blobs[s.code], f"{s.code}.parquet")
            for s in sorted(layers, key=lambda s: s.code)
        ]
        descriptors.append(_descriptor(SOURCES_TYPE, sources_json, "sources.json"))
        content = digest(
            json.dumps([d["digest"] for d in descriptors if d["mediaType"] == VALUES_TYPE]).encode()
        )
        if (
            existing is not None
            and annotations_of(existing).get(PREFIX + "content") == content
            and annotations_of(existing).get(PREFIX + "energy_version") == ENERGY_VERSION
        ):
            return YearResult(year, "unchanged", t, rows, failed)
        for s in layers:
            self.registry.push_blob(blobs[s.code])
        self.registry.push_blob(sources_json)
        start, end = year_bounds(year)
        manifest = {
            "schemaVersion": 2,
            "mediaType": MANIFEST,
            "artifactType": ARTIFACT_TYPE,
            "config": EMPTY,
            "layers": descriptors,
            "annotations": {
                "org.opencontainers.image.source": self.source_url,
                "org.opencontainers.image.created": self.clock().isoformat(timespec="seconds"),
                "org.opencontainers.image.licenses": LICENSE,
                "org.opencontainers.image.description": description(
                    "Energiedaten von PredictionDB aus freien Quellen (SMARD, Energy-Charts, EIA,"
                    " EZB, Weltbank, Oil Bulletin, Eurostat, Hub'eau, eigene Marktwerte), je"
                    " Kalenderjahr ein Paket (Tag JJJJ), ein Parquet je Quelle mit code, ts,"
                    " value, published, Rang und Lizenz je Quelle in sources.json."
                ),
                PREFIX + "attribution": ATTRIBUTION,
                PREFIX + "energy_version": ENERGY_VERSION,
                PREFIX + "year": t,
                PREFIX + "start": start.isoformat(),
                PREFIX + "end": end.isoformat(),
                PREFIX + "content": content,
                PREFIX + "rows": json.dumps(rows, sort_keys=True, separators=(",", ":")),
                PREFIX + "stale": ",".join(sorted(stale)),
            },
        }
        self.registry.put_manifest(t, manifest)
        return YearResult(year, "published", t, rows, failed)


def plan(
    registry: Registry, first: int, last: int, now: datetime, *, rebuild: bool = False
) -> list[int]:
    """Jahre, die fehlen, eine andere Version tragen oder noch Korrekturen bekommen.

    Das laufende Jahr ist immer dabei; das Vorjahr bis Ende Februar (späte Korrekturen und der
    Jahresmarktwert, der erst zwei Wochen nach Jahresende gilt).

    @relation(SWR-WXX-082, scope=function)
    """
    current = year_of(now)
    years = list(range(first, min(last, current) + 1))
    if rebuild:
        return years[::-1]
    existing = set(registry.tags())
    todo = []
    for y in years[::-1]:
        t = tag(y)
        if y == current or (y == current - 1 and now.astimezone(BERLIN).month <= 2):
            todo.append(y)
            continue
        if t not in existing:
            todo.append(y)
            continue
        m = registry.manifest(t)
        a = annotations_of(m) if m is not None else {}
        if a.get(PREFIX + "energy_version") != ENERGY_VERSION or a.get(PREFIX + "stale"):
            todo.append(y)
    return todo


def load_year(registry: Registry, year: int) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    """Werte je Quelle und ``sources.json`` eines Jahres.

    @relation(SWR-WXX-081, scope=function)
    """
    m = registry.manifest(tag(year))
    if m is None:
        raise LookupError(f"keine Energiedaten {registry.repository}:{tag(year)}")
    meta = old_sources(registry, m)
    values = {}
    for code, data in _old_layers(registry, m).items():
        if not _SOURCE.fullmatch(code):
            raise ArtifactError(f"Energie-Layer: ungültige Quelle {code!r}")
        values[code] = read_values(data)
    return values, meta


def load_best(
    registry: Registry, years: Iterable[int], codes: Iterable[str] | None = None
) -> pd.DataFrame:
    """``energy.value_best`` über mehrere Jahre, optional nur für ``codes``.

    @relation(SWR-WXX-081, scope=function)
    """
    want = set(codes) if codes is not None else None
    parts = []
    for y in years:
        values, meta = load_year(registry, y)
        ranked = []
        for code, df in values.items():
            prio = int(meta.get(code, {}).get("priority", 100))
            if want is not None:
                df = df[df["code"].isin(want)]
            ranked.append((prio, df))
        parts.append(value_best(ranked))
    if not parts:
        return empty_rows()
    df = pd.concat(parts, ignore_index=True)
    # Jahresgrenzen überlappen nicht; doppelte Zeitpunkte nur bei geänderter Zeitzone der Quelle
    return df.drop_duplicates(["code", "ts"], keep="last").reset_index(drop=True)
