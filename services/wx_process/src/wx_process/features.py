"""Gold: Wetter-Features je Anlagen-Cluster aus einem Silber-Datensatz.

Jedes Cluster hat eine Gewichtsliste „Anteil der Clusterleistung je Gitterzelle“
(``assets.grid_weight``, Summe 1). Lineare Größen sind das gewichtete Mittel über diese
Zellen. Nichtlineare Größen (Wind in Nabenhöhe, Leistungskurve, PV-Modell) werden erst je
Zelle gerechnet und dann gewichtet, denn die Leistung bei mittlerem Wind ist nicht die mittlere
Leistung.

Features je Art der Technologie (``config/process/*.yaml``):

========= ==================================================================================
``wind``  ``wind_speed_10m``, ``wind_speed_100m``, ``wind_speed_hub``, ``gust``, ``t2m``,
          ``wind_cf`` (Kapazitätsfaktor 0-1), ``wind_power`` (MW = cf mal Clusterleistung)
``pv``    ``ghi``, ``t2m``, ``cloud_cover``, ``snow_fraction``, ``pv_cf``, ``pv_power`` (MW)
``hydro`` ``precip``, ``t2m``, ``snow_fraction``
``other`` ``t2m``
========= ==================================================================================

Größen mit Intervallbezug (Strahlung, Niederschlag, Schneeanteil) gelten für das Intervall
bis zur Gültigkeitszeit, also die Stunde (bis 120 h) bzw. drei Stunden davor.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, Field

from wx_process.silver import Dataset

F64 = NDArray[np.float64]
Kind = Literal["wind", "pv", "hydro", "other"]

#: Version der Feature-Definitionen; eine Änderung lässt ``latest`` die Läufe neu rechnen
FEATURES_VERSION = "1"


class PowerCurve(BaseModel):
    """Normierte Leistungskurve: kubisch zwischen Einschalt- und Nenngeschwindigkeit."""

    cut_in: float = Field(default=3.0, gt=0)
    rated: float = Field(default=12.0, gt=0)
    cut_out: float = Field(default=25.0, gt=0)

    def __call__(self, v: F64) -> F64:
        cf = (v**3 - self.cut_in**3) / (self.rated**3 - self.cut_in**3)
        cf = np.clip(cf, 0.0, 1.0)
        cf = np.where((v < self.cut_in) | (v >= self.cut_out), 0.0, cf)
        return np.where(np.isnan(v), np.nan, cf)


class TechnologyFeatures(BaseModel):
    kind: Kind
    #: Nabenhöhe, wenn das MaStR für das Cluster keine liefert
    hub_height_m: float = Field(default=100.0, gt=10)
    power_curve: PowerCurve = Field(default_factory=PowerCurve)


class PvModel(BaseModel):
    """Einfaches PV-Modell auf Globalstrahlung (ohne Ausrichtung und Neigung)."""

    performance_ratio: float = Field(default=0.85, gt=0, le=1)
    #: relative Leistungsänderung je K Zelltemperatur über 25 °C
    temp_coeff: float = -0.004
    #: Nennbetriebstemperatur der Zelle (NOCT, bei 800 W/m² und 20 °C Luft)
    noct_c: float = 45.0

    def __call__(self, ghi: F64, t_air_c: F64) -> F64:
        t_cell = t_air_c + ghi * (self.noct_c - 20.0) / 800.0
        cf = self.performance_ratio * ghi / 1000.0 * (1.0 + self.temp_coeff * (t_cell - 25.0))
        return np.where(np.isnan(cf), np.nan, np.clip(cf, 0.0, None))


@dataclass(frozen=True)
class ClusterWeights:
    technology: str
    cluster_no: int
    region_id: int
    lat: F64
    lon: F64
    share: F64
    #: leistungsgewichtete Nabenhöhe aus dem MaStR (nur Wind), sonst ``None``
    hub_height_m: float | None = None


@dataclass(frozen=True)
class FeatureSeries:
    code: str
    unit: str
    region_id: int
    #: ein Wert je Schritt des Datensatzes (NaN = nicht berechenbar, z. B. Strahlung in f000)
    values: F64


#: Code → Einheit für ``meta.feature``
UNITS: dict[str, str] = {
    "t2m": "degC",
    "wind_speed_10m": "m s-1",
    "wind_speed_100m": "m s-1",
    "wind_speed_hub": "m s-1",
    "gust": "m s-1",
    "wind_cf": "1",
    "wind_power": "MW",
    "ghi": "W m-2",
    "cloud_cover": "%",
    "snow_fraction": "1",
    "pv_cf": "1",
    "pv_power": "MW",
    "precip": "mm h-1",
}

LN10 = math.log(10.0)


def cell_index(ds: Dataset, lat: F64, lon: F64) -> tuple[NDArray[np.int64], NDArray[np.bool_]]:
    """Flacher Index der Gitterzelle je Punkt und Maske „liegt auf dem Gitter“."""
    glat, glon = ds.grid.lat, ds.grid.lon
    dlat = abs(glat[1] - glat[0]) if len(glat) > 1 else 1.0
    dlon = abs(glon[1] - glon[0]) if len(glon) > 1 else 1.0
    lon = np.mod(np.asarray(lon, float) + 180.0, 360.0) - 180.0
    i = np.round((glat[0] - np.asarray(lat, float)) / dlat).astype(np.int64)
    j = np.round((lon - glon[0]) / dlon).astype(np.int64)
    ok = (i >= 0) & (i < len(glat)) & (j >= 0) & (j < len(glon))
    ic, jc = np.clip(i, 0, len(glat) - 1), np.clip(j, 0, len(glon) - 1)
    ok &= np.isclose(glat[ic], lat, atol=1e-3) & np.isclose(glon[jc], lon, atol=1e-3)
    return ic * len(glon) + jc, ok


class FeatureBuilder:
    def __init__(
        self,
        technologies: Mapping[str, TechnologyFeatures],
        pv: PvModel | None = None,
    ) -> None:
        self.technologies = technologies
        self.pv = pv or PvModel()

    def build(
        self,
        ds: Dataset,
        clusters: list[ClusterWeights],
        capacity_mw: Callable[[str, int], F64 | None] | None = None,
    ) -> tuple[list[FeatureSeries], dict[str, float]]:
        """Features aller Cluster; zweites Ergebnis: Anteil der Gewichte auf dem Gitter je Cluster.

        ``capacity_mw(technologie, cluster)`` liefert die installierte Leistung je Schritt
        (MW); ohne sie entfallen ``wind_power`` und ``pv_power``.
        """
        flat = {n: a.reshape(len(ds.steps), -1).astype(np.float64) for n, a in ds.data.items()}
        out: list[FeatureSeries] = []
        coverage: dict[str, float] = {}
        for c in clusters:
            tech = self.technologies.get(c.technology)
            if tech is None:
                continue
            idx, ok = cell_index(ds, c.lat, c.lon)
            total = float(c.share.sum())
            inside = float(c.share[ok].sum())
            coverage[f"{c.technology}:c{c.cluster_no:02d}"] = inside / total if total > 0 else 0.0
            if inside <= 0:
                continue
            w = c.share[ok] / inside
            cells = idx[ok]

            def field(name: str, cells: NDArray[np.int64] = cells) -> F64 | None:
                a = flat.get(name)
                return None if a is None else a[:, cells]

            def mean(x: F64, w: F64 = w) -> F64:
                r: F64 = x @ w
                return r

            cap = capacity_mw(c.technology, c.cluster_no) if capacity_mw else None
            values = self._cluster(tech, c, field, mean, cap)
            out.extend(
                FeatureSeries(code, UNITS[code], c.region_id, v) for code, v in values.items()
            )
        return out, coverage

    def _cluster(
        self,
        tech: TechnologyFeatures,
        c: ClusterWeights,
        field: Callable[[str], F64 | None],
        mean: Callable[[F64], F64],
        cap: F64 | None,
    ) -> dict[str, F64]:
        res: dict[str, F64] = {}
        t2m = field("t2m")
        t_c = None if t2m is None else t2m - 273.15
        if t_c is not None:
            res["t2m"] = mean(t_c)

        if tech.kind == "wind":
            u10, v10, u100, v100 = (field(n) for n in ("u10", "v10", "u100", "v100"))
            if u10 is not None and v10 is not None:
                ws10 = np.hypot(u10, v10)
                res["wind_speed_10m"] = mean(ws10)
            if u100 is not None and v100 is not None:
                ws100 = np.hypot(u100, v100)
                res["wind_speed_100m"] = mean(ws100)
                h = c.hub_height_m or tech.hub_height_m
                if u10 is not None and v10 is not None:
                    alpha = shear_exponent(ws10, ws100)
                else:
                    alpha = np.full_like(ws100, 1 / 7)
                ws_hub = ws100 * (h / 100.0) ** alpha
                res["wind_speed_hub"] = mean(ws_hub)
                cf = mean(tech.power_curve(ws_hub))
                res["wind_cf"] = cf
                if cap is not None:
                    res["wind_power"] = cf * cap
            gust = field("gust")
            if gust is not None:
                res["gust"] = mean(gust)

        elif tech.kind == "pv":
            ghi = field("dswrf")
            if ghi is not None:
                res["ghi"] = mean(ghi)
                if t_c is not None:
                    cf = mean(self.pv(ghi, t_c))
                    res["pv_cf"] = cf
                    if cap is not None:
                        res["pv_power"] = cf * cap
            tcc = field("tcc")
            if tcc is not None:
                res["cloud_cover"] = mean(tcc)
            snow = field("csnow_mean")
            if snow is not None:
                res["snow_fraction"] = mean(snow)

        elif tech.kind == "hydro":
            tp = field("tp")
            if tp is not None:
                res["precip"] = mean(tp)
            snow = field("csnow_mean")
            if snow is not None:
                res["snow_fraction"] = mean(snow)
        return res


def shear_exponent(ws10: F64, ws100: F64) -> F64:
    """Hellmann-Exponent aus 10 m und 100 m; bei Schwachwind 1/7, begrenzt auf 0 bis 0,6."""
    with np.errstate(divide="ignore", invalid="ignore"):
        alpha = np.log(ws100 / ws10) / LN10
    alpha = np.where((ws10 < 0.5) | (ws100 < 0.5) | ~np.isfinite(alpha), 1 / 7, alpha)
    alpha = np.clip(alpha, 0.0, 0.6)
    return np.where(np.isnan(ws10) | np.isnan(ws100), np.nan, alpha)
