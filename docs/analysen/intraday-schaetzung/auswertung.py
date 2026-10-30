"""Intraday schätzbar? ID-AEP (Netztransparenz, nur Referenz) gegen Day-Ahead + Prognosefehler.

Aufruf (siehe README): uv run --no-project --with pandas --with numpy --with lightgbm
--with scikit-learn python auswertung.py ORDNER
ORDNER enthält ntp.csv und energy_qh.csv.gz. Schreibt nur aggregierte Tabellen nach ORDNER/out/.
"""

import io
import sys
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

d = Path(sys.argv[1])
out = d / "out"
out.mkdir(exist_ok=True)

# --- Netztransparenz: zwei Abschnitte mit eigenem Header
text = (d / "ntp.csv").read_text(encoding="utf-8")
lines = text.splitlines()
split = next(i for i, ln in enumerate(lines) if i > 0 and ln.startswith("endpoint;"))
ida = pd.read_csv(io.StringIO("\n".join(lines[:split])), sep=";", dtype=str)
reb = pd.read_csv(io.StringIO("\n".join(lines[split:])), sep=";", dtype=str)


def num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.str.replace(".", "", regex=False).str.replace(",", ".", regex=False), errors="coerce"
    )


print(
    "Zeitzonen ID-AEP:",
    ida.iloc[:, 3].value_counts().to_dict(),
    " reBAP:",
    reb["Zeitzone"].value_counts().to_dict(),
)
ida_ts = pd.to_datetime(
    ida.iloc[:, 1] + " " + ida.iloc[:, 2], utc=True, dayfirst=False, errors="coerce"
)
idaep = pd.Series(num(ida.iloc[:, 6]).values, index=ida_ts, name="id").dropna()
idaep = idaep[~idaep.index.duplicated()]
reb_ts = pd.to_datetime(reb["Datum"] + " " + reb["von"], utc=True, dayfirst=True, errors="coerce")
rebap = pd.Series(num(reb["reBAP unterdeckt"]).values, index=reb_ts, name="rebap").dropna()
rebap = rebap[~rebap.index.duplicated()]
print(
    f"ID-AEP {len(idaep)} Werte {idaep.index.min()} .. {idaep.index.max()}, "
    f"NaN-Zeitstempel {ida_ts.isna().sum()}"
)
print(f"reBAP {len(rebap)} Werte {rebap.index.min()} .. {rebap.index.max()}")

# --- Energiedaten auf 15-min-Raster
e = pd.read_csv(d / "energy_qh.csv.gz", parse_dates=["ts"])
e["ts"] = pd.to_datetime(e["ts"], utc=True)
p = e.pivot_table(index="ts", columns="code", values="value", aggfunc="mean")
grid = pd.date_range(idaep.index.min(), idaep.index.max(), freq="15min", tz="UTC")
p = p.reindex(p.index.union(grid)).sort_index().ffill(limit=3).reindex(grid)
df = p.join(idaep).join(rebap)
df = df.rename(columns={"da_price_de_lu": "da"})
df["spread"] = df["id"] - df["da"]
wind_act = df["gen_wind_onshore"] + df["gen_wind_offshore"]
wind_fc = df["gen_da_forecast_wind_onshore"] + df["gen_da_forecast_wind_offshore"]
df["err_wind"] = (wind_act - wind_fc) / 1000
df["err_solar"] = (df["gen_solar"] - df["gen_da_forecast_solar"]) / 1000
df["err_load"] = (df["load_actual"] - df["load_da_forecast"]) / 1000
df["err_res"] = (
    df["err_load"] - df["err_wind"] - df["err_solar"]
)  # GW, + = mehr Bedarf als erwartet
df["res_fc"] = (df["load_da_forecast"] - wind_fc - df["gen_da_forecast_solar"]) / 1000
hour_mean = df["res_fc"].groupby(df.index.floor("h")).transform("mean")
df["res_fc_intrahour"] = df["res_fc"] - hour_mean
loc = df.index.tz_convert("Europe/Berlin")
df["hour"] = loc.hour
df["qh"] = loc.minute // 15
df["month"] = loc.month
df["dow"] = loc.dayofweek
df["da_qh"] = (df.index >= pd.Timestamp("2025-10-01 00:00", tz="Europe/Berlin")).astype(int)
df["year"] = loc.year
need = ["id", "da", "err_wind", "err_solar", "err_load", "res_fc"]
cov = df[need].notna().mean().round(3).to_dict()
print("Abdeckung:", cov)
df = df.dropna(subset=need)
print(f"Nutzbare Viertelstunden: {len(df)}")


# --- 1) Wie weit liegt Intraday überhaupt vom Day-Ahead weg?
def stats(g: pd.DataFrame) -> pd.Series:
    s = g["spread"]
    return pd.Series(
        {
            "n": len(g),
            "da_mittel": g["da"].mean(),
            "id_mittel": g["id"].mean(),
            "spread_mittel": s.mean(),
            "MAE_id_vs_da": s.abs().mean(),
            "std_spread": s.std(),
            "p10": s.quantile(0.1),
            "p90": s.quantile(0.9),
            "p01": s.quantile(0.01),
            "p99": s.quantile(0.99),
            "corr_id_da": g["id"].corr(g["da"]),
            "MAE_tag_base": (
                g.groupby(g.index.tz_convert("Europe/Berlin").date)["spread"].mean().abs().mean()
            ),
        }
    )


t1 = df.groupby("year").apply(stats).round(2)
t1.loc["gesamt"] = stats(df).round(2)
t1.to_csv(out / "t1_abstand_je_jahr.csv")
print("\n=== T1 Abstand ID-AEP zu Day-Ahead je Jahr (EUR/MWh)\n", t1.to_string())

# --- 2) Zusammenhang Spread ~ Prognosefehler Residuallast (gebinnt)
bins = [-np.inf, -6, -4, -2, -1, 0, 1, 2, 4, 6, np.inf]
df["bin"] = pd.cut(df["err_res"], bins)
t2 = df.groupby("bin", observed=True)["spread"].agg(
    ["count", "mean", "median", lambda s: s.quantile(0.1), lambda s: s.quantile(0.9)]
)
t2.columns = ["n", "mittel", "median", "p10", "p90"]
t2 = t2.round(2)
t2.to_csv(out / "t2_spread_je_fehlerklasse.csv")
print("\n=== T2 Spread je Fehlerklasse Residuallast (GW, Ist minus DA-Prognose)\n", t2.to_string())
corr = (
    df[["spread", "err_res", "err_wind", "err_solar", "err_load", "res_fc_intrahour"]]
    .corr()["spread"]
    .round(3)
)
print("\nKorrelation mit Spread:", corr.to_dict())

# --- 3) Modelle, Zeit-Split
train = df[df.index < pd.Timestamp("2025-01-01", tz="UTC")]
test = df[df.index >= pd.Timestamp("2025-01-01", tz="UTC")]
res: dict[str, dict[str, float]] = {}


def score(name: str, pred_spread: np.ndarray, part: pd.DataFrame = test) -> None:
    err = part["spread"] - pred_spread
    sst = ((part["spread"] - part["spread"].mean()) ** 2).sum()
    tag = (err.groupby(part.index.tz_convert("Europe/Berlin").date).mean()).abs().mean()
    res[name] = {
        "MAE_qh": err.abs().mean(),
        "RMSE_qh": np.sqrt((err**2).mean()),
        "R2_spread": 1 - (err**2).sum() / sst,
        "MAE_tag_base": tag,
        "vorzeichen_treffer": (np.sign(pred_spread) == np.sign(part["spread"])).mean()
        if np.ndim(pred_spread)
        else np.nan,
    }


score("B0 ID = DA", np.zeros(len(test)))
prof = train.groupby(["da_qh", "hour", "qh"])["spread"].mean()
fallback = train.groupby(["hour", "qh"])["spread"].mean()
key = list(zip(test["da_qh"], test["hour"], test["qh"], strict=True))
b1 = np.array([prof.get(k, fallback.get(k[1:], 0.0)) for k in key])
score("B1 DA + Tagesprofil", b1)


def lin_x(part: pd.DataFrame) -> pd.DataFrame:
    x = pd.DataFrame(
        {
            "err_wind": part["err_wind"],
            "err_solar": part["err_solar"],
            "err_load": part["err_load"],
            "err_res_x_da": part["err_res"] * part["da"].clip(-50, 500) / 100,
            "intra": part["res_fc_intrahour"] * (1 - part["da_qh"]),
        }
    )
    return x.join(pd.get_dummies(part["hour"], prefix="h", dtype=float))


def design(part: pd.DataFrame) -> np.ndarray:
    x = lin_x(part).reindex(columns=lin_x(train).columns, fill_value=0.0)
    return np.column_stack([np.ones(len(x)), x.to_numpy(dtype=float)])


beta = np.linalg.lstsq(design(train), train["spread"].to_numpy(dtype=float), rcond=None)[0]
score("L1 linear Prognosefehler", design(test) @ beta)
coef = pd.Series(beta[1:], index=lin_x(train).columns)
print(
    "\nL1-Koeffizienten (EUR/MWh je GW):",
    coef[[c for c in coef.index if not c.startswith("h_")]].round(2).to_dict(),
)


feat = [
    "da",
    "err_wind",
    "err_solar",
    "err_load",
    "err_res",
    "res_fc",
    "res_fc_intrahour",
    "hour",
    "qh",
    "month",
    "dow",
    "da_qh",
]
g = lgb.LGBMRegressor(
    n_estimators=600,
    learning_rate=0.03,
    num_leaves=63,
    min_child_samples=200,
    objective="l1",
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    verbose=-1,
)
g.fit(train[feat], train["spread"])
score("G1 LightGBM Prognosefehler", np.asarray(g.predict(test[feat])))
feat0 = ["da", "res_fc", "res_fc_intrahour", "hour", "qh", "month", "dow", "da_qh"]
g0 = lgb.LGBMRegressor(
    n_estimators=600,
    learning_rate=0.03,
    num_leaves=63,
    min_child_samples=200,
    objective="l1",
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    verbose=-1,
)
g0.fit(train[feat0], train["spread"])
score("G0 LightGBM ohne Ist-Werte (nur vorab bekannt)", np.asarray(g0.predict(test[feat0])))
t3 = pd.DataFrame(res).T.round(3)
t3.to_csv(out / "t3_modelle_test_2025_2026.csv")
print(
    f"\n=== T3 Testzeitraum {test.index.min()} .. {test.index.max()} ({len(test)} qh), "
    f"Training ab {train.index.min()}\n",
    t3.to_string(),
)
imp = pd.Series(g.booster_.feature_importance("gain"), index=feat)
print(
    "Wichtigkeit G1 (Gain-Anteil):",
    (imp / imp.sum()).round(3).sort_values(ascending=False).to_dict(),
)

# Fehler nach Zeitraum im Test (G1 vs B0)
test = test.assign(g1=g.predict(test[feat]))
t4 = (
    test.groupby([test.index.tz_convert("Europe/Berlin").year, "da_qh"])
    .apply(
        lambda x: pd.Series(
            {
                "n": len(x),
                "MAE_B0": x["spread"].abs().mean(),
                "MAE_G1": (x["spread"] - x["g1"]).abs().mean(),
            }
        )
    )
    .round(2)
)
t4.to_csv(out / "t4_test_je_jahr.csv")
print("\n=== T4 Test je Jahr/DA-Auflösung\n", t4.to_string())
t5 = (
    test.groupby("hour")
    .apply(
        lambda x: pd.Series(
            {
                "MAE_B0": x["spread"].abs().mean(),
                "MAE_G1": (x["spread"] - x["g1"]).abs().mean(),
                "spread_mittel": x["spread"].mean(),
            }
        )
    )
    .round(2)
)
t5.to_csv(out / "t5_test_je_stunde.csv")
print("\n=== T5 Test je Stunde (Ortszeit)\n", t5.to_string())

# --- 4) reBAP am Rand
both = df.dropna(subset=["rebap"])
print(
    f"\nreBAP vs ID-AEP: corr {both['rebap'].corr(both['id']):.3f}, "
    f"MAE {(both['rebap'] - both['id']).abs().mean():.1f}, "
    f"corr(reBAP-ID, err_res) {(both['rebap'] - both['id']).corr(both['err_res']):.3f}"
)
