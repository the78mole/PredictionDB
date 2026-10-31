"""Synthetischer Snapshot für Tests: Last, Wind, PV und Preis mit bekanntem Zusammenhang.

Die Last folgt Tagesgang, Wochenende, Feiertagen und Temperatur, Wind und PV hängen an
Wetterreihen und wachsendem Bestand, der Preis an der Residuallast. Wetterläufe (00Z) haben
GFS-Schritte (stündlich bis 120 h, dann dreistündlich) und einen mit dem Vorlauf wachsenden
Fehler.

@relation(SWA-MLT-TESTING, scope=file)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ml_train.calendar import holiday_share
from ml_train.snapshot import Snapshot

H = pd.Timedelta(hours=1)


def synthetic_snapshot(
    start: str = "2022-01-01",
    end: str = "2024-01-01",
    weather_from: str | None = "2023-01-01",
    seed: int = 7,
) -> Snapshot:
    rng = np.random.default_rng(seed)
    ts = pd.date_range(start, end, freq="h", tz="UTC", inclusive="left")
    n = len(ts)
    local = ts.tz_convert("Europe/Berlin")
    doy = local.dayofyear.to_numpy()
    hour = local.hour.to_numpy()
    days = pd.DatetimeIndex(local.normalize().tz_localize(None))

    # Wetter "Wahrheit": Temperatur, Wind (AR(1)), Strahlung
    temp = 10 - 9 * np.cos(2 * np.pi * (doy - 15) / 365) + 4 * np.sin(2 * np.pi * (hour - 9) / 24)
    temp += np.convolve(rng.normal(0, 1.0, n), np.ones(48) / 7, mode="same")
    wind = np.empty(n)
    w = 6.0
    for i in range(n):
        w = 0.97 * w + 0.03 * 6.5 + rng.normal(0, 0.6)
        wind[i] = max(w, 0.0)
    elev = np.sin(np.pi * (hour - 6 + 2 * np.cos(2 * np.pi * (doy - 172) / 365)) / 12)
    clear = np.clip(elev, 0, None) * (0.6 + 0.4 * np.cos(2 * np.pi * (doy - 172) / 365))
    cloud = np.clip(np.convolve(rng.uniform(0, 1, n), np.ones(12) / 12, mode="same"), 0, 1)
    ghi = 900 * clear * (1 - 0.7 * cloud)

    years = (ts - ts[0]) / pd.Timedelta(days=365)
    cap_wind = 60000 + 3000 * np.asarray(years)
    cap_pv = 60000 + 15000 * np.asarray(years)
    wind_cf = np.clip((wind**3 - 27) / (1728 - 27), 0, 1)
    gen_wind = cap_wind * wind_cf * 0.9 + rng.normal(0, 800, n)
    gen_solar = cap_pv * ghi / 1000 * 0.8 + rng.normal(0, 300, n)

    hs = holiday_share(pd.DatetimeIndex(days))
    weekend = np.asarray(local.dayofweek >= 5)
    shape = 1 + 0.15 * np.sin(np.pi * np.clip(hour - 5, 0, 17) / 17)
    load = 52000 * shape - 7000 * weekend - 8000 * hs + 350 * np.clip(15 - temp, 0, None)
    load += rng.normal(0, 900, n)
    resid = load - gen_wind - gen_solar
    price = 20 + 0.0018 * resid + rng.normal(0, 6, n)
    # Fossile Erzeugung nach Merit-Order: Braunkohle zuerst, dann Steinkohle, dann Gas
    thermal = np.clip(resid, 0, None)
    lignite = np.clip(thermal, 3000, 15000)
    hard_coal = np.clip(thermal - 15000, 500, 12000)
    gas = np.clip(thermal - 27000, 1000, 25000)

    energy = pd.concat(
        [
            pd.DataFrame({"ts": ts, "code": code, "value": v, "published": ts + 2 * H})
            for code, v in {
                "load_actual": load,
                "load_da_forecast": load + rng.normal(0, 1500, n),
                "gen_wind_onshore": gen_wind,
                "gen_solar": np.clip(gen_solar, 0, None),
                "da_price_de_lu": price,
                "da_price_at": price + 5 + rng.normal(0, 4, n),
                "residual_load": resid,
                "gen_lignite": lignite,
                "gen_hard_coal": hard_coal,
                "gen_gas": gas,
            }.items()
        ],
        ignore_index=True,
    )
    # Monatswert (Gaspreis) mit Veröffentlichung eine Woche nach Monatsende
    months = pd.date_range(start, end, freq="MS", tz="UTC", inclusive="left")
    gas = pd.DataFrame(
        {
            "ts": months,
            "code": "gas_eu_m",
            "value": 10 + np.arange(len(months), dtype=float),
            "published": (months + pd.offsets.MonthBegin(1)) + pd.Timedelta(days=7),
        }
    )
    # Monatsprognose je Ausgabe (wie EIA STEO): Ausgabe i erwartet je Monat Vorlauf eine
    # Änderung von +-5 %, im Wechsel; veröffentlicht am 11. des Ausgabemonats
    sign = np.where(np.arange(len(months)) % 2 == 0, 1.0, -1.0)
    outlook = pd.concat(
        [
            pd.DataFrame(
                {
                    "ts": months,
                    "code": f"steo_brent_h{k:02d}",
                    "value": (60 + np.arange(len(months))) * np.exp(0.05 * k * sign),
                    "published": months + pd.Timedelta(days=10),
                }
            )
            for k in range(4)
        ],
        ignore_index=True,
    )
    energy = pd.concat([energy, gas, outlook], ignore_index=True)

    cap_days = pd.date_range(start, end, freq="D")
    cyears = np.asarray((cap_days - cap_days[0]) / pd.Timedelta(days=365))
    capacity = pd.concat(
        [
            pd.DataFrame(
                {
                    "day": cap_days.date,
                    "technology": "wind_on",
                    "capacity_mw": 60000 + 3000 * cyears,
                }
            ),
            pd.DataFrame(
                {
                    "day": cap_days.date,
                    "technology": "pv_roof",
                    "capacity_mw": 40000 + 10000 * cyears,
                }
            ),
            pd.DataFrame(
                {
                    "day": cap_days.date,
                    "technology": "pv_ground",
                    "capacity_mw": 20000 + 5000 * cyears,
                }
            ),
        ],
        ignore_index=True,
    )

    wx_rows = []
    if weather_from is not None:
        truth = pd.DataFrame(
            {
                "pv_roof_t2m": temp,
                "wind_on_wind_speed_hub": wind,
                "pv_roof_ghi": ghi,
                "wind_on_wind_power": cap_wind * wind_cf,
            },
            index=ts,
        )
        steps = np.r_[np.arange(0, 121), np.arange(123, 217, 3)]
        for run in pd.date_range(weather_from, end, freq="D", tz="UTC", inclusive="left"):
            valid = run + pd.to_timedelta(steps, unit="h")
            ok = valid < ts[-1]
            v = truth.reindex(valid[ok])
            noise = (1 + steps[ok] / 48)[:, None] * rng.normal(0, 1, (ok.sum(), 4))
            v = v + noise * np.array([0.8, 0.6, 30, 1500])
            v["pv_roof_ghi"] = v["pv_roof_ghi"].clip(lower=0)
            long = v.reset_index(names="valid_time").melt(
                id_vars="valid_time", var_name="feature", value_name="value"
            )
            long["run_time"] = run
            wx_rows.append(long)
    weather = (
        pd.concat(wx_rows, ignore_index=True)[["run_time", "valid_time", "feature", "value"]]
        if wx_rows
        else pd.DataFrame(columns=["run_time", "valid_time", "feature", "value"])
    )
    manifest = {"synthetic": True, "range": [start, end], "created": "synthetic"}
    snap = Snapshot(energy, weather, capacity, manifest)
    snap.manifest["snapshot_id"] = "synthetic"
    return snap
