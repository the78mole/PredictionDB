"""Energie- und Preisreihen direkt von den freien Quellen, mit kleinem Datei-Cache statt Datenbank.

Die Quellen sind dieselben Klassen wie in ``energy-ingest`` (``energy_ingest.sources``), nur ohne
Datenbank: Je Reihen-Code gilt dieselbe Rangfolge wie in ``energy.value_best`` (SMARD vor
Energy-Charts), fällt eine Quelle aus, springt die nächste ein. Unfreie Quellen kommen nicht
vor, damit die Prognose dieselben Eingänge hat wie ein veröffentlichtes Modell.

Der Cache (``cache/energy/<code>.parquet``) hält Werte in der Auflösung der Quelle mit
``published`` (Leakage-Schutz wie im Training, beim Überschreiben bleibt der früheste
Zeitpunkt). Je Lauf werden nur das Fenster um heute (je nach Art der Reihe einige Tage bis
Wochen) und fehlende Historie geladen. Die Marktwerte nach EEG rechnet der Dienst wie
``pdb_marktwert`` aus den SMARD-Stundenwerten.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from energy_ingest.config import EnergySettings
from energy_ingest.http import Http
from energy_ingest.model import (
    BERLIN,
    NEIGHBOUR_ZONES,
    Kind,
    SeriesData,
    next_period,
    published_at,
    series,
)
from energy_ingest.sources import Source
from energy_ingest.sources.ecb import Ecb
from energy_ingest.sources.eia import FREE_SERIES, Eia, EiaBulk
from energy_ingest.sources.energy_charts import FORECASTS, PUBLIC_POWER, EnergyCharts
from energy_ingest.sources.marktwert import PRICE, WEIGHTS, market_values, period_starts
from energy_ingest.sources.oil_bulletin import PRODUCTS, OilBulletin
from energy_ingest.sources.smard import FILTERS, Smard
from energy_ingest.sources.worldbank import COLUMNS, WorldBank
from predictiondb_core.log import get_logger

log = get_logger(__name__)

H = timedelta(hours=1)
MARKET_VALUE_SOURCE = "pdb_marktwert"
MARKET_VALUE_LICENSE = (
    "Eigene Berechnung nach Anlage 1 EEG aus freien Quellen (CC BY 4.0, Bundesnetzagentur | "
    "SMARD.de)"
)

#: Fenster vor heute, das je Lauf neu geladen wird (Nachmeldungen, Korrekturen, Verzug)
REFRESH: dict[Kind, timedelta] = {
    Kind.PRICE_DA: timedelta(days=3),
    Kind.ACTUAL: timedelta(days=3),
    Kind.LOAD_DA_FORECAST: timedelta(days=3),
    Kind.GEN_DA_FORECAST: timedelta(days=3),
    Kind.PRICE_DAILY: timedelta(days=14),
    Kind.PRICE_WEEKLY: timedelta(days=28),
    Kind.PRICE_MONTHLY: timedelta(days=70),
}


def routes() -> dict[str, list[tuple[str, str]]]:
    """Reihen-Code -> ``(quelle, datensatz)`` in der Rangfolge von ``energy.value_best``."""
    out: dict[str, list[tuple[str, str]]] = {}

    def add(code: str, source: str, dataset: str) -> None:
        out.setdefault(code, []).append((source, dataset))

    for f in FILTERS:
        add(f.code, "smard", f.code)
    for code in FREE_SERIES:
        add(code, "eia", "prices")
    add("fx_usd_eur", "ecb", "fx")
    for code, _unit in COLUMNS.values():
        add(code, "worldbank", "prices")
    for product in PRODUCTS.values():
        for suffix in ("", "_net"):
            add(f"retail_de_{product}{suffix}", "oil_bulletin", "retail_de")
    add("da_price_de_lu", "energy_charts", "price")
    for short in NEIGHBOUR_ZONES.values():
        add(f"da_price_{short}", "energy_charts", f"price_{short}")
    for code in PUBLIC_POWER.values():
        add(code, "energy_charts", "public_power")
    for ptype, code in FORECASTS.items():
        add(code, "energy_charts", f"forecast_{ptype}")
    return out


def is_market_value(code: str) -> bool:
    return code.startswith(("mw_", "jw_"))


def build_sources(http: Http, settings: EnergySettings | None = None) -> dict[str, Source]:
    s = settings or EnergySettings()
    sources: list[Source] = [
        Smard(http),
        EnergyCharts(http),
        Ecb(http),
        Eia(http, s.eia_api_key.get_secret_value(), EiaBulk(http)),
        WorldBank(http, s.worldbank_url),
        OilBulletin(http, s.oil_bulletin_url),
    ]
    return {src.code: src for src in sources}


@dataclass
class Window:
    start: datetime
    end: datetime


class EnergyCache:
    """Hält die Reihen eines Modellpakets aktuell und liefert sie im Snapshot-Format."""

    def __init__(self, root: Path, sources: Mapping[str, Source]) -> None:
        self.root = root / "energy"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sources = sources
        self.routes = routes()
        self._state_path = self.root / "_state.json"
        self.state: dict[str, dict[str, str]] = (
            json.loads(self._state_path.read_text()) if self._state_path.exists() else {}
        )

    # --- Ablage ------------------------------------------------------------------------------

    def _path(self, code: str) -> Path:
        return self.root / f"{code}.parquet"

    def read(self, code: str) -> pd.DataFrame:
        p = self._path(code)
        if not p.exists():
            return pd.DataFrame(columns=["ts", "value", "published", "source"])
        return pd.read_parquet(p)

    def merge(self, code: str, new: pd.DataFrame) -> None:
        old = self.read(code)
        if new.empty:
            return
        both = pd.concat([old, new], ignore_index=True) if not old.empty else new
        both["ts"] = pd.to_datetime(both["ts"], utc=True)
        both["published"] = pd.to_datetime(both["published"], utc=True)
        first_pub = both.groupby("ts")["published"].min()
        both = both.drop_duplicates("ts", keep="last").set_index("ts")
        both["published"] = first_pub
        both = both.reset_index().sort_values("ts")
        both.to_parquet(self._path(code), index=False)

    def _save_state(self) -> None:
        self._state_path.write_text(json.dumps(self.state, indent=1, sort_keys=True))

    # --- Abruf -------------------------------------------------------------------------------

    def _windows(self, code: str, need: Window, now: datetime) -> list[Window]:
        st = self.state.get(code)
        refresh = REFRESH.get(series(code).kind, timedelta(days=3))
        if st is None:
            return [need]
        have_from = datetime.fromisoformat(st["from"])
        have_to = datetime.fromisoformat(st["to"])
        out = []
        if need.start < have_from:
            out.append(Window(need.start, have_from))
        recent = max(need.start, min(now - refresh, have_to))
        if recent < need.end:
            out.append(Window(recent, need.end))
        return out

    def _fetch(self, source: Source, dataset: str, w: Window) -> list[SeriesData]:
        out: list[SeriesData] = []
        step = source.max_chunk(dataset)
        t = w.start
        while t < w.end:
            nxt = min(t + step, w.end)
            out.extend(source.fetch(dataset, t, nxt))
            t = nxt
        return out

    def _store(
        self,
        batch: Iterable[SeriesData],
        source: str,
        fetched: datetime,
        wanted: Iterable[str] | None = None,
    ) -> set[str]:
        keep = None if wanted is None else set(wanted)
        got = set()
        for sd in batch:
            if not sd.points or (keep is not None and sd.code not in keep):
                continue
            ts = [p[0] for p in sd.points]
            df = pd.DataFrame(
                {
                    "ts": ts,
                    "value": [p[1] for p in sd.points],
                    "published": [
                        published_at(sd.info.kind, t, sd.resolution, fetched) for t in ts
                    ],
                    "source": source,
                }
            )
            self.merge(sd.code, df)
            got.add(sd.code)
        return got

    def update(self, needs: Mapping[str, Window], now: datetime | None = None) -> dict[str, str]:
        """Lädt fehlende und frische Werte; Ergebnis: Code -> Fehlertext für nicht abrufbare."""
        now = now or datetime.now(UTC)
        errors: dict[str, str] = {}
        plain = {c: w for c, w in needs.items() if not is_market_value(c)}
        # Marktwerte brauchen die Stundenwerte von Preis und Erzeugung über ganze Perioden
        mv = {c: w for c, w in needs.items() if is_market_value(c)}
        if mv:
            start = min(w.start for w in mv.values())
            local = start.astimezone(BERLIN)
            year_start = datetime(local.year, 1, 1, tzinfo=BERLIN).astimezone(UTC)
            for base in (PRICE, *WEIGHTS):
                w = plain.get(base)
                end = max(now, w.end) if w else now
                plain[base] = Window(min(year_start, w.start if w else year_start), end)

        # je (Quelle, Datensatz, Fenster) ein Abruf; Codes ohne Ergebnis gehen zur nächsten Quelle
        unreachable: set[str] = set()
        pending = {c: list(self.routes.get(c, [])) for c in plain}
        for code, r in pending.items():
            if not r:
                errors[code] = "keine freie Quelle bekannt"
        while True:
            jobs: dict[tuple[str, str, datetime, datetime], list[str]] = {}
            for code, r in pending.items():
                if not r:
                    continue
                for w in self._windows(code, plain[code], now):
                    jobs.setdefault((r[0][0], r[0][1], w.start, w.end), []).append(code)
            if not jobs:
                break
            failed: set[str] = set()
            done: set[str] = set()
            for (src, dataset, a, b), codes in jobs.items():
                source = self.sources.get(src)
                if source is None or src in unreachable:
                    failed.update(codes)
                    continue
                try:
                    batch = self._fetch(source, dataset, Window(a, b))
                    got = self._store(batch, src, now, codes)
                except Exception as e:  # eine Quelle darf die anderen nicht aufhalten
                    log.warning("energy.fetch_failed", source=src, dataset=dataset, error=str(e))
                    if isinstance(e, httpx.TransportError):
                        # nicht erreichbar: in diesem Lauf nicht weiter warten, nächste Quelle
                        unreachable.add(src)
                    for c in codes:
                        errors[c] = f"{src}/{dataset}: {e}"
                    failed.update(codes)
                    continue
                # leere Historie ist kein Fehler, leeres Fenster bis heute schon
                for c in codes:
                    if c in got or b < now - timedelta(days=7):
                        done.add(c)
                    else:
                        failed.add(c)
                        errors.setdefault(c, f"{src}/{dataset}: keine Werte")
            for c in done - failed:
                w = plain[c]
                st = self.state.get(c)
                new_from = min(w.start, datetime.fromisoformat(st["from"])) if st else w.start
                new_to = max(min(w.end, now), datetime.fromisoformat(st["to"])) if st else now
                self.state[c] = {"from": new_from.isoformat(), "to": new_to.isoformat()}
                errors.pop(c, None)
                pending[c] = []
            for c in failed - done:
                pending[c] = pending[c][1:]
            self._save_state()

        for code, w in mv.items():
            try:
                self._market_value(code, w.start, now)
                errors.pop(code, None)
            except Exception as e:
                errors[code] = f"{MARKET_VALUE_SOURCE}: {e}"
        for code, err in errors.items():
            log.warning("energy.unavailable", code=code, error=err)
        return errors

    def _hourly(self, codes: Iterable[str]) -> list[tuple[datetime, str, float]]:
        rows: list[tuple[datetime, str, float]] = []
        for code in codes:
            df = self.read(code)
            if df.empty:
                continue
            h = df.assign(ts=pd.to_datetime(df["ts"], utc=True).dt.floor("h"))
            h = h.groupby("ts")["value"].mean()
            rows += [(t.to_pydatetime(), code, float(v)) for t, v in h.items()]
        return rows

    def _market_value(self, code: str, start: datetime, now: datetime) -> None:
        months = 1 if code.startswith("mw_") else 12
        periods = [p for p in period_starts(start, now, months) if next_period(p, months) <= now]
        rows = self._hourly([PRICE, *WEIGHTS])
        for sd in market_values(rows, periods, months):
            if sd.code == code:
                self._store([sd], MARKET_VALUE_SOURCE, now)

    # --- Snapshot ----------------------------------------------------------------------------

    def snapshot_energy(self, needs: Mapping[str, Window]) -> pd.DataFrame:
        """Stundenmittel je Code wie ``ml_train.snapshot.ENERGY_SQL`` (spätestes ``published``)."""
        parts = []
        for code, w in needs.items():
            df = self.read(code)
            if df.empty:
                continue
            df = df.assign(ts=pd.to_datetime(df["ts"], utc=True))
            df = df[(df["ts"] >= pd.Timestamp(w.start)) & (df["ts"] < pd.Timestamp(w.end))]
            if df.empty:
                continue
            g = df.assign(ts=df["ts"].dt.floor("h")).groupby("ts")
            h = pd.DataFrame({"value": g["value"].mean(), "published": g["published"].max()})
            parts.append(h.reset_index().assign(code=code))
        if not parts:
            return pd.DataFrame(columns=["ts", "code", "value", "published"])
        out = pd.concat(parts, ignore_index=True)[["ts", "code", "value", "published"]]
        out["published"] = pd.to_datetime(out["published"], utc=True)
        return out

    def sources_used(self, codes: Iterable[str]) -> dict[str, str]:
        """Quelle -> Lizenz der Werte, die in die Prognose eingehen."""
        used: dict[str, str] = {}
        for code in codes:
            df = self.read(code)
            for src in df["source"].unique() if not df.empty else []:
                if src == MARKET_VALUE_SOURCE:
                    used[src] = MARKET_VALUE_LICENSE
                elif src in self.sources:
                    used[src] = self.sources[src].license
        return used
