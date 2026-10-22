"""Kalender-Merkmale je Stunde (Ortszeit Europe/Berlin).

Feiertage zählen als Anteil der Bevölkerung, für die der Tag ein Feiertag ist (gesetzliche
Feiertage je Land aus dem Paket ``holidays``, Bevölkerung 2023 nach Destatis, gerundet).
Brückentage sind Werktage zwischen einem Feiertag und dem Wochenende. Schulferien fehlen noch.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

import holidays
import numpy as np
import pandas as pd

BERLIN = "Europe/Berlin"

#: Bevölkerung je Land in Mio. (Destatis, Stand 31.12.2023, gerundet)
POPULATION: dict[str, float] = {
    "BW": 11.28,
    "BY": 13.37,
    "BE": 3.76,
    "BB": 2.57,
    "HB": 0.69,
    "HH": 1.89,
    "HE": 6.39,
    "MV": 1.63,
    "NI": 8.14,
    "NW": 18.14,
    "RP": 4.16,
    "SL": 0.99,
    "SN": 4.09,
    "ST": 2.19,
    "SH": 2.95,
    "TH": 2.13,
}

CALENDAR_COLUMNS = [
    "hour",
    "dow",
    "weekend",
    "month",
    "doy_sin",
    "doy_cos",
    "holiday_share",
    "holiday_national",
    "bridge_day",
    "christmas",
    "dst",
    "day_len_h",
]


@lru_cache(maxsize=64)
def _holiday_share_year(year: int) -> dict[date, float]:
    total = sum(POPULATION.values())
    share: dict[date, float] = {}
    for land, pop in POPULATION.items():
        for d in holidays.country_holidays("DE", subdiv=land, years=year):
            share[d] = share.get(d, 0.0) + pop / total
    return share


def holiday_share(days: pd.DatetimeIndex) -> np.ndarray:
    out = np.zeros(len(days))
    for i, d in enumerate(days.date):
        out[i] = _holiday_share_year(d.year).get(d, 0.0)
    return np.minimum(out, 1.0)


def daily_calendar(start: date, end: date) -> pd.DataFrame:
    """Tagesmerkmale von ``start`` bis ``end`` (je einschließlich), Index Ortsdatum."""
    days = pd.date_range(start - timedelta(days=3), end + timedelta(days=3), freq="D")
    hs = holiday_share(days)
    dow = days.dayofweek.to_numpy()
    off = (hs >= 0.5) | (dow >= 5)
    # Brückentag: Werktag ohne Feiertag, links und rechts frei (Feiertag oder Wochenende)
    prev_off = np.r_[False, off[:-1]]
    next_off = np.r_[off[1:], False]
    near_holiday = (np.r_[0.0, hs[:-1]] >= 0.5) | (np.r_[hs[1:], 0.0] >= 0.5)
    bridge = (~off) & prev_off & next_off & near_holiday
    md = days.month * 100 + days.day
    christmas = (md >= 1224) | (md <= 101)
    df = pd.DataFrame(
        {
            "holiday_share": hs,
            "holiday_national": (hs >= 0.999).astype(np.int8),
            "bridge_day": bridge.astype(np.int8),
            "christmas": christmas.astype(np.int8),
        },
        index=days.date,
    )
    out: pd.DataFrame = df.loc[start:end]
    return out


def calendar_features(ts_utc: pd.DatetimeIndex | pd.Series) -> pd.DataFrame:
    """Merkmale je Zeitpunkt (UTC, Stundenbeginn); Ergebnis in derselben Reihenfolge."""
    idx = pd.DatetimeIndex(ts_utc)
    local = idx.tz_convert(BERLIN)
    local_days = pd.DatetimeIndex(local.normalize().tz_localize(None))
    daily = daily_calendar(local_days.min().date(), local_days.max().date())
    d = daily.reindex(local_days.date)
    doy = local.dayofyear.to_numpy()
    utc_off = (local.tz_localize(None) - idx.tz_localize(None)).total_seconds().to_numpy() / 3600
    # Tageslänge als glatter Saisonindikator (astronomisch, 51° N)
    decl = 23.44 * np.sin(np.radians(360 / 365 * (doy - 81)))
    cos_h = -np.tan(np.radians(51.0)) * np.tan(np.radians(decl))
    day_len = 2 * np.degrees(np.arccos(np.clip(cos_h, -1, 1))) / 15
    return pd.DataFrame(
        {
            "hour": local.hour.to_numpy(),
            "dow": local.dayofweek.to_numpy(),
            "weekend": (local.dayofweek >= 5).astype(np.int8),
            "month": local.month.to_numpy(),
            "doy_sin": np.sin(2 * np.pi * doy / 365.25),
            "doy_cos": np.cos(2 * np.pi * doy / 365.25),
            "holiday_share": d["holiday_share"].to_numpy(),
            "holiday_national": d["holiday_national"].to_numpy(),
            "bridge_day": d["bridge_day"].to_numpy(),
            "christmas": d["christmas"].to_numpy(),
            "dst": (utc_off > 1.5).astype(np.int8),
            "day_len_h": day_len,
        },
        index=idx,
    )
