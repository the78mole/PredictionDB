"""Zweiter Lauf: Robustheit (Ausreißer, Zeitversatz, Stundenmittel, nur 15-min-DA-Zeit).

Aufruf wie auswertung.py; nutzt dessen Datenaufbereitung (import), schreibt nur Aggregate.
"""
import contextlib
import io
import sys

import numpy as np
import pandas as pd

with contextlib.redirect_stdout(io.StringIO()):
    import auswertung as a  # noqa: E402  (rechnet Lauf 1 still mit)
from sklearn.linear_model import HuberRegressor, LinearRegression

df = a.df
pd.set_option("display.width", 200)

# Anteil der Streuung aus den extremsten Viertelstunden
s = df["spread"]
sq = (s - s.mean()) ** 2
top = sq.sort_values(ascending=False)
print("Anteil Varianz der größten 0,1/1/5 % Viertelstunden:",
      {q: round(top.iloc[: int(len(top) * q / 100)].sum() / sq.sum(), 3) for q in (0.1, 1, 5)})
print("Median |Spread|:", round(s.abs().median(), 2), " MAD-Spread:", round((s - s.median()).abs().median(), 2))

# Zeitversatz: Korrelation Spread mit err_res um k Viertelstunden verschoben
lag = {k: round(df["spread"].corr(df["err_res"].shift(k)), 3) for k in range(-8, 9, 2)}
print("corr(spread, err_res.shift(k)):", lag)
lagda = {k: round(df["id"].corr(df["da"].shift(k)), 3) for k in (-4, -2, -1, 0, 1, 2, 4)}
print("corr(id, da.shift(k)):", lagda)
print("Spearman id~da je Jahr:", df.groupby("year").apply(lambda g: round(g["id"].corr(g["da"], method="spearman"), 3)).to_dict())
print("Spearman spread~err_res je Jahr:", df.groupby("year").apply(lambda g: round(g["spread"].corr(g["err_res"], method="spearman"), 3)).to_dict())

# Gekappter Spread (1./99. Perzentil des Trainings)
train, test = a.train, a.test
lo, hi = train["spread"].quantile([0.01, 0.99])
HCOLS = [f"h_{i}" for i in range(24)]


def X(part):
    x = a.lin_x(part)
    base = x[[c for c in x.columns if not c.startswith("h_")]]
    return base.join(x.reindex(columns=HCOLS, fill_value=0.0)[HCOLS]).astype(float)




def metr(name, y, pred, tbl):
    e = y - pred
    sst = ((y - y.mean()) ** 2).sum()
    tbl[name] = {"MAE": e.abs().mean(), "MedAE": e.abs().median(), "R2": 1 - (e ** 2).sum() / sst}


tbl = {}
yc = test["spread"].clip(lo, hi)
metr("B0 ID=DA (gekappt)", yc, 0, tbl)
lr = LinearRegression().fit(X(train), train["spread"].clip(lo, hi))
metr("L1 linear (gekappt)", yc, lr.predict(X(test)), tbl)
hub = HuberRegressor(max_iter=500).fit(X(train).astype(float), train["spread"])
metr("L2 Huber (gekappt bewertet)", yc, hub.predict(X(test).astype(float)), tbl)
metr("B0 ID=DA (roh)", test["spread"], 0, tbl)
metr("L2 Huber (roh)", test["spread"], hub.predict(X(test).astype(float)), tbl)
print("\n=== R1 Test 2025-01..2026-10, Spread gekappt auf", round(lo, 1), "..", round(hi, 1), "\n",
      pd.DataFrame(tbl).T.round(3).to_string())
print("Huber-Koeffizienten:", pd.Series(hub.coef_, index=X(train).columns)[["err_wind", "err_solar", "err_load", "err_res_x_da", "intra"]].round(2).to_dict())

# Stundenmittel (näher an ID1/ID3-artigen Stundenindizes)
h = df[["id", "da", "err_wind", "err_solar", "err_load", "err_res", "res_fc"]].resample("h").mean().dropna()
h["spread"] = h["id"] - h["da"]
h["hour"] = h.index.tz_convert("Europe/Berlin").hour
h["da_qh"] = 0
h["res_fc_intrahour"] = 0.0
htr, hte = h[h.index < "2025-01-01"], h[h.index >= "2025-01-01"]
tblh = {}
metr("B0 ID=DA", hte["spread"], 0, tblh)
hl = HuberRegressor(max_iter=500).fit(X(htr).astype(float), htr["spread"])
metr("L2 Huber Prognosefehler", hte["spread"], hl.predict(X(hte).astype(float)), tblh)
print("\n=== R2 Stundenmittel, Test 2025-01..2026-10 (", len(hte), "h)\n", pd.DataFrame(tblh).T.round(3).to_string())
print("Stunden: corr(id,da)", round(h["id"].corr(h["da"]), 3), " std spread", round(h["spread"].std(), 2),
      " MAE id vs da", round(h["spread"].abs().mean(), 2))

# Nur 15-min-DA-Zeit: Training 2025-10..2026-04, Test 2026-05..
q = df[df["da_qh"] == 1]
qtr, qte = q[q.index < "2026-05-01"], q[q.index >= "2026-05-01"]
if qte.empty:
    sys.exit(0)
tblq = {}
metr("B0 ID=DA", qte["spread"], 0, tblq)
hq = HuberRegressor(max_iter=500).fit(X(qtr).astype(float), qtr["spread"])
metr("L2 Huber Prognosefehler", qte["spread"], hq.predict(X(qte).astype(float)), tblq)
metr("L2 aus Altzeit (Huber gesamt)", qte["spread"], hub.predict(X(qte).astype(float)), tblq)
print(f"\n=== R3 Nur 15-min-Day-Ahead: Training {len(qtr)} qh, Test {len(qte)} qh (ab 2026-05)\n",
      pd.DataFrame(tblq).T.round(3).to_string())
print("Huber-Koeffizienten 15-min-Zeit:", pd.Series(hq.coef_, index=X(qtr).columns)[["err_wind", "err_solar", "err_load", "err_res_x_da"]].round(2).to_dict())

# Spread-Klassen im Test: Trefferquote der Richtung bei großem Fehler
big = test[test["err_res"].abs() > 3]
print(f"\n|err_res|>3 GW im Test: {len(big)} qh ({len(big)/len(test):.1%}), Richtung Spread = Richtung Fehler in "
      f"{(np.sign(big['spread']) == np.sign(big['err_res'])).mean():.1%}, mittlerer |Spread| {big['spread'].abs().mean():.1f}")
