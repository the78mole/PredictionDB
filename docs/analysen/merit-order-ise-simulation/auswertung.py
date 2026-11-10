"""Merit-Order-Angebotskurven aus dem Speichersimulator von Energy-Charts.

Die Seite „Simulierte Börsenstrompreise“ rechnet je ISO-Woche 2000 Speichervarianten vor
(100 Leistungsstufen, 5 Speicherdauern, 4 Szenarien). Jede Variante verschiebt die Nachfrage
je Marktzeiteinheit um die Batterieleistung und liefert den Preis, der sich dann auf den
EPEX-Kurven einstellt. Aus diesen Paaren (Zusatznachfrage, Preis) ergibt sich je
Marktzeiteinheit ein Stück der Angebotskurve; über die Woche zusammengelegt und über der
Residuallast aufgetragen die Merit-Order der Woche.

Die Wochendateien tragen keine Lizenzangabe und beruhen auf EPEX-Daten. Sie bleiben im Cache
(``--cache``), ins Repo schreibt das Skript nur aggregierte Kennzahlen. Aufruf::

    uv run --no-project --with numpy --with matplotlib --with pyyaml \
        python docs/analysen/merit-order-ise-simulation/auswertung.py --cache /tmp/ec-sim
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import itertools
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml

BERLIN = ZoneInfo("Europe/Berlin")
HIER = Path(__file__).resolve().parent
TAB = HIER / "tabellen"
GRA = HIER / "grafiken"

SZENARIO_SUFFIX = {
    "": "base",
    "market coupling": "mc",
    "block bids": "cb",
    "market coupling + block bids": "mc_cb",
}
SZENARIO_NAME = {
    "base": "nur EPEX-Kurven",
    "mc": "mit Austausch",
    "cb": "mit Blockgeboten",
    "mc_cb": "mit Austausch und Blockgeboten",
}
RE_SIM = re.compile(
    r"^(DA price \(DE-LU\) with|Battery power) ([\d.]+) GW / (\d) h battery(?:, (.+))?$"
)
UMSTELLUNG_15MIN = dt.datetime(2025, 10, 1, tzinfo=dt.UTC) - dt.timedelta(hours=2)


@dataclass
class Woche:
    jahr: int
    kw: int
    start: dt.datetime
    mtu_min: int  # 60, 15 oder 0 (Woche mit dem Wechsel am 01.10.2025)
    zeit: list[dt.datetime]  # Beginn jeder Marktzeiteinheit, UTC
    last: np.ndarray
    resid: np.ndarray  # eigene Residuallast, GW
    resid_ec: np.ndarray  # Residuallast laut Energy-Charts, GW
    import_netto: np.ndarray  # GW, Import positiv
    p0: np.ndarray
    # Szenario -> Liste (w GW, Preis) je Variante, je Array der Länge n
    sim: dict[str, list[tuple[np.ndarray, np.ndarray]]] = field(default_factory=dict)


def lade_job() -> tuple[dict[str, Any], str]:
    raw = (HIER / "job.yaml").read_bytes()
    return yaml.safe_load(raw), hashlib.sha256(raw).hexdigest()


def iso_wochen(von: str, bis: str) -> list[tuple[int, int]]:
    def parse(s: str) -> dt.date:
        j, w = s.split("-W")
        return dt.date.fromisocalendar(int(j), int(w), 1)

    d, ende = parse(von), parse(bis)
    out = []
    while d <= ende:
        j, w, _ = d.isocalendar()
        out.append((j, w))
        d += dt.timedelta(days=7)
    return out


def hole(url: str, ziel: Path) -> bool:
    if ziel.exists():
        try:
            gzip.decompress(ziel.read_bytes())
            return True
        except (OSError, EOFError):
            ziel.unlink()  # abgebrochener Download, neu holen
    for versuch in range(4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                daten = r.read()
            tmp = ziel.with_suffix(".tmp")
            tmp.write_bytes(daten)
            gzip.decompress(daten)  # vollständig?
            tmp.replace(ziel)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            time.sleep(2**versuch)
        except (urllib.error.URLError, TimeoutError, OSError, EOFError):
            time.sleep(2**versuch)
    raise RuntimeError(f"Abruf fehlgeschlagen: {url}")


def name_en(s: dict[str, Any]) -> str:
    n = s["name"]
    if isinstance(n, list):
        n = n[0]
    return str(n["en"]) if isinstance(n, dict) else str(n)


def arr(s: dict[str, Any]) -> np.ndarray:
    return np.array([np.nan if v is None else v for v in s["data"]], dtype=float)


def lies_woche(pfad: Path, jahr: int, kw: int) -> Woche | None:
    reihen = json.loads(gzip.decompress(pfad.read_bytes()))
    n = {name_en(s): s for s in reihen}
    x = np.array(reihen[0]["xAxisValues"], dtype=float)
    p0 = arr(n["Day Ahead Auction (DE-LU)"])
    if len(x) < 668 or np.isnan(p0).any():
        return None  # Woche unvollständig
    start = dt.datetime.fromtimestamp(x[0] / 1000, tz=dt.UTC)
    # Bis 30.09.2025 stündliche Auktion: die Datei wiederholt den Stundenwert viermal.
    n_h = int((x < UMSTELLUNG_15MIN.timestamp() * 1000).sum())
    n_h -= n_h % 4

    def verdichte(a: np.ndarray) -> np.ndarray:
        return np.concatenate([a[:n_h].reshape(-1, 4).mean(axis=1), a[n_h:]])

    zeit = [
        dt.datetime.fromtimestamp(t / 1000, tz=dt.UTC) for t in np.concatenate([x[:n_h:4], x[n_h:]])
    ]

    last = arr(n["Load"])
    wind = arr(n["Wind onshore (Grid feed-in potential)"]) + arr(
        n["Wind offshore (Grid feed-in potential)"]
    )
    pv = arr(n["Solar (Grid feed-in potential)"])
    w = Woche(
        jahr=jahr,
        kw=kw,
        start=start,
        mtu_min=60 if n_h == len(x) else (15 if n_h == 0 else 0),
        zeit=zeit,
        last=verdichte(last) / 1000,
        resid=verdichte(last - wind - pv) / 1000,
        resid_ec=verdichte(arr(n["Residual load"])) / 1000,
        import_netto=verdichte(arr(n["Cross border electricity trading"])) / 1000,
        p0=verdichte(p0),
    )
    preis: dict[tuple[str, str, str], np.ndarray] = {}
    leistung: dict[tuple[str, str, str], np.ndarray] = {}
    for name, s in n.items():
        m = RE_SIM.match(name)
        if not m:
            continue
        key = (SZENARIO_SUFFIX[(m.group(4) or "").lower()], m.group(2), m.group(3))
        (preis if m.group(1).startswith("DA") else leistung)[key] = verdichte(arr(s))
    for key, p in preis.items():
        if key not in leistung:
            continue
        zusatz = -leistung[key] / 1000  # Laden = zusätzliche Nachfrage
        w.sim.setdefault(key[0], []).append((zusatz, p))
    return w


def punkte(w: Woche, sz: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Alle Punkte (MTU-Index, w, Residuallast + w, Preis) mit Gewicht 1 je MTU."""
    var = w.sim[sz]
    n = len(w.p0)
    idx = np.concatenate([np.arange(n)] * (len(var) + 1))
    zus = np.concatenate([np.zeros(n)] + [v[0] for v in var])
    pr = np.concatenate([w.p0] + [v[1] for v in var])
    ok = np.isfinite(zus) & np.isfinite(pr) & np.isfinite(w.resid[idx])
    return idx[ok], zus[ok], w.resid[idx[ok]] + zus[ok], pr[ok]


class Isoton:
    """Gewichtete isotone Regression (Pool Adjacent Violators) auf 0,1-GW-Klassen."""

    def __init__(self, x: np.ndarray, y: np.ndarray, g: np.ndarray, breite: float = 0.1) -> None:
        k = np.floor(x / breite).astype(np.int64)
        kl, inv = np.unique(k, return_inverse=True)
        gs = np.bincount(inv, weights=g)
        ys = np.bincount(inv, weights=g * y) / gs
        xs = np.bincount(inv, weights=g * x) / gs
        werte: list[float] = []
        gew: list[float] = []
        anz: list[int] = []
        for yi, gi in zip(ys, gs, strict=True):
            werte.append(float(yi))
            gew.append(float(gi))
            anz.append(1)
            while len(werte) > 1 and werte[-2] > werte[-1]:
                g2 = gew[-2] + gew[-1]
                werte[-2] = (werte[-2] * gew[-2] + werte[-1] * gew[-1]) / g2
                gew[-2] = g2
                anz[-2] += anz[-1]
                del werte[-1], gew[-1], anz[-1]
        self.x = xs
        self.y = np.repeat(np.array(werte), anz)
        assert len(kl) == len(self.y)

    def predict(self, x: Any) -> np.ndarray:
        out: np.ndarray = np.interp(np.asarray(x, dtype=float), self.x, self.y)
        return out


def wochenkurve(w: Woche, sz: str) -> tuple[Isoton, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    idx, zus, xs, pr = punkte(w, sz)
    gewicht = 1.0 / np.bincount(idx, minlength=len(w.p0))[idx]
    iso = Isoton(xs, pr, gewicht)
    return iso, idx, zus, xs, pr


def steigungen(w: Woche, sz: str, wmin: float, wmax: float) -> tuple[np.ndarray, np.ndarray]:
    """Lokale Steigung €/MWh je GW je MTU, getrennt nach Laden und Entladen (NaN ohne Punkte)."""
    n = len(w.p0)
    auf_z, auf_n = np.zeros(n), np.zeros(n)
    ab_z, ab_n = np.zeros(n), np.zeros(n)
    for zus, pr in w.sim[sz]:
        dp = pr - w.p0
        ok = np.isfinite(zus) & np.isfinite(dp) & (np.abs(zus) >= wmin) & (np.abs(zus) <= wmax)
        auf = ok & (zus > 0)
        ab = ok & (zus < 0)
        auf_z[auf] += (dp * zus)[auf]
        auf_n[auf] += (zus**2)[auf]
        ab_z[ab] += (dp * zus)[ab]
        ab_n[ab] += (zus**2)[ab]
    with np.errstate(invalid="ignore", divide="ignore"):
        return auf_z / auf_n, ab_z / ab_n


def schreibe_csv(pfad: Path, zeilen: list[dict[str, Any]]) -> None:
    with pfad.open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(zeilen[0].keys()), lineterminator="\n")
        wr.writeheader()
        for z in zeilen:
            wr.writerow({k: (round(v, 2) if isinstance(v, float) else v) for k, v in z.items()})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, required=True)
    args = ap.parse_args()
    gestartet = dt.datetime.now(dt.UTC)
    job, job_sha = lade_job()
    m = job["method"]
    src = job["inputs"]["energy_charts_simulation"]
    args.cache.mkdir(parents=True, exist_ok=True)
    TAB.mkdir(exist_ok=True)
    GRA.mkdir(exist_ok=True)
    klassen = [float(k) for k in m["preisklassen_eur_mwh"]]
    stuetz = [float(s) for s in m["stuetzstellen_gw"]]
    wmin, wmax = 0.05, 2.0

    wochen_tab: list[dict[str, Any]] = []
    kurven_tab: list[dict[str, Any]] = []
    steig_w_tab: list[dict[str, Any]] = []
    # Sammeln für die Steigung je Preisklasse: Szenario -> (p0, Steigung auf, Steigung ab)
    steig_sammel: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
    kurven_mem: dict[tuple[int, int, str], tuple[np.ndarray, np.ndarray]] = {}
    hashes: dict[str, str] = {}
    fehlend: list[str] = []
    unvollstaendig: list[str] = []
    n_mtu = 0
    n_punkte = 0
    n_resid_fehlt = 0

    for jahr, kw in iso_wochen(src["wochen"]["von"], src["wochen"]["bis"]):
        name = f"week_15min_{jahr}_{kw}.json.gz"
        pfad = args.cache / name
        if not hole(src["url"].format(jahr=jahr, kw=kw), pfad):
            fehlend.append(f"{jahr}-W{kw:02d}")
            continue
        hashes[name] = hashlib.sha256(pfad.read_bytes()).hexdigest()
        w = lies_woche(pfad, jahr, kw)
        if w is None:
            unvollstaendig.append(f"{jahr}-W{kw:02d}")
            continue
        n_mtu += len(w.p0)
        ortstag = np.array([t.astimezone(BERLIN).date().toordinal() for t in w.zeit])
        gueltig = np.isfinite(w.resid)
        n_resid_fehlt += int((~gueltig).sum())
        spannen = [np.ptp(w.p0[ortstag == t]) for t in np.unique(ortstag)]
        zeile: dict[str, Any] = {
            "jahr": jahr,
            "kw": kw,
            "montag": (w.start + dt.timedelta(hours=2)).date().isoformat(),
            "mtu_min": w.mtu_min,
            "preis_mittel": float(w.p0.mean()),
            "preis_min": float(w.p0.min()),
            "preis_max": float(w.p0.max()),
            "tagesspanne_mittel": float(np.mean(spannen)),
            "residuallast_mittel_gw": float(w.resid.mean()),
            "residuallast_min_gw": float(w.resid.min()),
            "residuallast_max_gw": float(w.resid.max()),
            "import_netto_mittel_gw": float(w.import_netto.mean()),
        }
        for sz in ("base", "mc", "cb", "mc_cb"):
            iso, _idx, _zus, xs, _pr = wochenkurve(w, sz)
            n_punkte += len(xs)
            p_ist = w.p0[gueltig]
            fehler = p_ist - iso.predict(w.resid[gueltig])
            zeile[f"mae_{sz}"] = float(np.abs(fehler).mean())
            zeile[f"r2_{sz}"] = float(1 - (fehler**2).sum() / ((p_ist - p_ist.mean()) ** 2).sum())
            for s in stuetz:
                lo, hi = np.percentile(xs, [0.5, 99.5])
                zeile[f"p{int(s)}_{sz}"] = float(iso.predict([s])[0]) if lo <= s <= hi else ""
            auf, ab = steigungen(w, sz, wmin, wmax)
            steig_sammel.setdefault(sz, []).append((w.p0, auf, ab))
            beide = np.concatenate([auf[np.isfinite(auf)], ab[np.isfinite(ab)]])
            zeile[f"steigung_median_{sz}"] = float(np.median(beide)) if len(beide) else ""
            steig_w_tab.append(
                {
                    "jahr": jahr,
                    "kw": kw,
                    "szenario": sz,
                    "mtu_laden": int(np.isfinite(auf).sum()),
                    "steigung_laden_median": float(np.nanmedian(auf))
                    if np.isfinite(auf).any()
                    else "",
                    "mtu_entladen": int(np.isfinite(ab).sum()),
                    "steigung_entladen_median": float(np.nanmedian(ab))
                    if np.isfinite(ab).any()
                    else "",
                }
            )
            lo, hi = np.percentile(xs, [0.5, 99.5])
            raster = np.arange(np.ceil(lo), np.floor(hi) + 1, 1.0)
            werte = iso.predict(raster)
            kurven_mem[(jahr, kw, sz)] = (raster, werte)
            for r, v in zip(raster, werte, strict=True):
                kurven_tab.append(
                    {
                        "jahr": jahr,
                        "kw": kw,
                        "szenario": sz,
                        "residuallast_gw": float(r),
                        "preis": float(v),
                    }
                )
        wochen_tab.append(zeile)
        print(
            f"{jahr}-W{kw:02d} p={zeile['preis_mittel']:.1f} "
            f"R={zeile['residuallast_mittel_gw']:.1f} "
            f"MAE base {zeile['mae_base']:.1f} mc_cb {zeile['mae_mc_cb']:.1f}",
            flush=True,
        )

    # Repräsentative Wochen
    def wahl(schluessel: str, groesste: bool) -> dict[str, Any]:
        return sorted(wochen_tab, key=lambda z: z[schluessel], reverse=groesste)[0]

    regeln = [
        ("höchste mittlere Residuallast", wahl("residuallast_mittel_gw", True)),
        ("niedrigste mittlere Residuallast", wahl("residuallast_mittel_gw", False)),
        ("höchster mittlerer Preis", wahl("preis_mittel", True)),
        ("niedrigster mittlerer Preis", wahl("preis_mittel", False)),
        ("größte Tagesspanne im Mittel", wahl("tagesspanne_mittel", True)),
        ("letzte vollständige Woche", wochen_tab[-1]),
    ]
    rep: list[tuple[str, dict[str, Any]]] = []
    for grund, z in regeln:
        vorhanden = [r for r in rep if r[1] is z]
        if vorhanden:
            idx_r = rep.index(vorhanden[0])
            rep[idx_r] = (vorhanden[0][0] + "; " + grund, z)
        else:
            rep.append((grund, z))
    rep_tab = [
        {
            "jahr": z["jahr"],
            "kw": z["kw"],
            "montag": z["montag"],
            "grund": g,
            "preis_mittel": z["preis_mittel"],
            "residuallast_mittel_gw": z["residuallast_mittel_gw"],
            **{f"p{int(s)}_mc_cb": z[f"p{int(s)}_mc_cb"] for s in stuetz},
            **{f"p{int(s)}_base": z[f"p{int(s)}_base"] for s in stuetz},
            "steigung_median_base": z["steigung_median_base"],
            "steigung_median_mc_cb": z["steigung_median_mc_cb"],
            "mae_mc_cb": z["mae_mc_cb"],
            "r2_mc_cb": z["r2_mc_cb"],
        }
        for g, z in rep
    ]

    # Steigung je Preisklasse über alle Wochen
    steig_tab: list[dict[str, Any]] = []
    for sz, teile in steig_sammel.items():
        p0 = np.concatenate([t[0] for t in teile])
        auf = np.concatenate([t[1] for t in teile])
        ab = np.concatenate([t[2] for t in teile])
        for von, bis in itertools.pairwise(klassen):
            k = (p0 >= von) & (p0 < bis)
            a, b = auf[k & np.isfinite(auf)], ab[k & np.isfinite(ab)]
            steig_tab.append(
                {
                    "szenario": sz,
                    "von": von,
                    "bis": bis,
                    "mtu": int(k.sum()),
                    "preis_median": float(np.median(p0[k])) if k.any() else "",
                    "mtu_laden": len(a),
                    "steigung_laden_median": float(np.median(a)) if len(a) else "",
                    "steigung_laden_p25": float(np.percentile(a, 25)) if len(a) else "",
                    "steigung_laden_p75": float(np.percentile(a, 75)) if len(a) else "",
                    "mtu_entladen": len(b),
                    "steigung_entladen_median": float(np.median(b)) if len(b) else "",
                    "steigung_entladen_p25": float(np.percentile(b, 25)) if len(b) else "",
                    "steigung_entladen_p75": float(np.percentile(b, 75)) if len(b) else "",
                }
            )

    schreibe_csv(TAB / "wochen.csv", wochen_tab)
    schreibe_csv(TAB / "kurven.csv", kurven_tab)
    schreibe_csv(TAB / "steigung_preisniveau.csv", steig_tab)
    schreibe_csv(TAB / "steigung_wochen.csv", steig_w_tab)
    schreibe_csv(TAB / "repraesentativ.csv", rep_tab)

    detail = {}
    for z in (rep[0][1], rep[1][1]):
        dw = lies_woche(
            args.cache / f"week_15min_{z['jahr']}_{z['kw']}.json.gz", z["jahr"], z["kw"]
        )
        assert dw is not None
        detail[(z["jahr"], z["kw"])] = dw
    grafiken(wochen_tab, kurven_mem, rep, steig_tab, detail, stuetz)

    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, cwd=HIER
    ).stdout.strip()
    info = {
        "job": job["id"],
        "job_sha256": job_sha,
        "git_commit": commit,
        "gestartet": gestartet.isoformat(timespec="seconds"),
        "beendet": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "quelle_abgerufen": gestartet.date().isoformat(),
        "zeitraum": {"von": wochen_tab[0]["montag"], "bis_montag": wochen_tab[-1]["montag"]},
        "umfang": {
            "wochen": len(wochen_tab),
            "wochen_stundenraster": sum(1 for z in wochen_tab if z["mtu_min"] == 60),
            "wochen_viertelstundenraster": sum(1 for z in wochen_tab if z["mtu_min"] == 15),
            "wochen_gemischt": [
                f"{z['jahr']}-W{z['kw']:02d}" for z in wochen_tab if z["mtu_min"] == 0
            ],
            "marktzeiteinheiten": n_mtu,
            "punkte_alle_szenarien": n_punkte,
            "varianten_je_woche": 2000,
        },
        "wochen_nicht_veroeffentlicht": fehlend,
        "wochen_unvollstaendig": unvollstaendig,
        "fehlende_werte_ersetzt": 0,
        "marktzeiteinheiten_ohne_residuallast_ausgelassen": n_resid_fehlt,
        "schritte_zeitlich_interpoliert": 0,
        "eingaben_sha256": hashes,
        "parameter_der_quelle": {
            "wirkungsgrad_rundlauf": 0.86,
            "leistungsstufen": 100,
            "leistung_gw": [0.1, 20.0],
            "dauer_h": [2, 3, 4, 5, 6],
            "szenarien": list(SZENARIO_NAME),
            "methodik": "Gandhi, Fraunhofer ISE, v1.0 vom 24.09.2026",
        },
        "annahmen": {
            "stundenwerte": "bis 30.09.2025 vier gleiche Viertelstundenwerte je Stunde verdichtet",
            "steigung_bereich_gw": [wmin, wmax],
            "residuallast": m["residuallast"],
        },
    }
    (TAB / "lauf.json").write_text(json.dumps(info, indent=1, ensure_ascii=False) + "\n")


def grafiken(
    wochen_tab: list[dict[str, Any]],
    kurven_mem: dict[tuple[int, int, str], tuple[np.ndarray, np.ndarray]],
    rep: list[tuple[str, dict[str, Any]]],
    steig_tab: list[dict[str, Any]],
    detail_mem: dict[tuple[int, int], Woche],
    stuetz: list[float],
) -> None:
    farben = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf", "#8c564b"]

    # 1. Wochenkurven
    fig, axe = plt.subplots(1, 2, figsize=(13, 5.2), sharey=True)
    for ax, sz in zip(axe, ("base", "mc_cb"), strict=True):
        for z in wochen_tab:
            r, v = kurven_mem[(z["jahr"], z["kw"], sz)]
            ax.plot(r, v, color="#bbbbbb", lw=0.6, zorder=1)
        for i, (grund, z) in enumerate(rep):
            r, v = kurven_mem[(z["jahr"], z["kw"], sz)]
            ax.plot(
                r,
                v,
                color=farben[i % len(farben)],
                lw=2,
                zorder=2,
                label=f"KW {z['kw']}/{z['jahr']} ({grund.split(';')[0]})",
            )
        ax.set_title(f"Szenario {sz}: {SZENARIO_NAME[sz]}")
        ax.set_xlabel("Residuallast + Zusatznachfrage Speicher (GW)")
        ax.grid(alpha=0.3)
        ax.set_ylim(-150, 450)
    axe[0].set_ylabel("Day-Ahead-Preis DE-LU (€/MWh)")
    axe[1].legend(fontsize=7.5, loc="upper left")
    fig.suptitle(
        "Wochen-Angebotskurven aus dem Energy-Charts-Speichersimulator (grau: alle Wochen)"
    )
    fig.tight_layout()
    fig.savefig(GRA / "kurven_wochen.png", dpi=130)
    plt.close(fig)

    # 2. Detail: zwei gegensätzliche Wochen, Punktwolke base mit Segmenten
    paar = [rep[0][1], rep[1][1]]
    fig, axe = plt.subplots(1, 2, figsize=(13, 5.2))
    for ax, z in zip(axe, paar, strict=True):
        w = detail_mem[(z["jahr"], z["kw"])]
        _iso, _idx, _zus, xs, pr = wochenkurve(w, "base")
        sel = np.random.default_rng(0).choice(len(xs), size=min(len(xs), 40000), replace=False)
        ax.scatter(
            xs[sel], pr[sel], s=1, color="#999999", alpha=0.25, label="simulierte Punkte (base)"
        )
        ax.scatter(w.resid, w.p0, s=6, color="black", label="Ist-Preis")
        for sz, f in (("base", "#1f77b4"), ("mc_cb", "#d62728")):
            r, v = kurven_mem[(z["jahr"], z["kw"], sz)]
            ax.plot(r, v, color=f, lw=2, label=f"Wochenkurve {sz}")
        ax.set_title(f"KW {z['kw']}/{z['jahr']} (ab {z['montag']})")
        ax.set_xlabel("Residuallast + Zusatznachfrage (GW)")
        ax.set_ylabel("€/MWh")
        lo, hi = np.percentile(pr, [0.5, 99.5])
        ax.set_ylim(min(-50, lo), max(200, hi))
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(GRA / "woche_detail.png", dpi=130)
    plt.close(fig)

    # 3. Steigung je Preisklasse, Vergleich mit Variante D (PR #234)
    fig, ax = plt.subplots(figsize=(9, 5))
    vgl = HIER.parent / "batteriespeicher-2025-ise" / "tabellen" / "steigung_preisniveau.csv"
    for sz, f in (("base", "#1f77b4"), ("mc_cb", "#d62728")):
        zs = [z for z in steig_tab if z["szenario"] == sz and z["mtu"] > 20]
        x = [z["preis_median"] for z in zs]
        for seite, ls in (("laden", "-"), ("entladen", "--")):
            y = [z[f"steigung_{seite}_median"] if z[f"mtu_{seite}"] >= 20 else np.nan for z in zs]
            y = [np.nan if v == "" else v for v in y]
            ax.plot(x, y, ls, marker="o", color=f, label=f"{sz}, {seite}")
    if vgl.exists():
        with vgl.open() as fh:
            d = list(csv.DictReader(fh))
        ax.plot(
            [float(r["preis_median"]) for r in d],
            [float(r["steigung_median"]) for r in d],
            ":",
            marker="s",
            color="black",
            label="Variante D (PR #234, Nachbarstunden 2025)",
        )
    ax.set_yscale("log")
    ax.set_xlabel("Ist-Preis der Marktzeiteinheit (Median der Klasse, €/MWh)")
    ax.set_ylabel("lokale Steigung (€/MWh je GW)")
    ax.set_title("Steigung der Angebotskurve nach Preisniveau")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(GRA / "steigung_preisniveau.png", dpi=130)
    plt.close(fig)

    # 4. Preis bei fester Residuallast über die Wochen
    fig, ax = plt.subplots(figsize=(11, 4.8))
    tage = [dt.date.fromisoformat(z["montag"]) for z in wochen_tab]
    for i, s in enumerate(stuetz):
        y = [np.nan if z[f"p{int(s)}_mc_cb"] == "" else z[f"p{int(s)}_mc_cb"] for z in wochen_tab]
        ax.plot(tage, y, marker=".", color=farben[i % len(farben)], label=f"{int(s)} GW")
    ax.plot(
        tage,
        [z["preis_mittel"] for z in wochen_tab],
        color="black",
        lw=1,
        ls=":",
        label="Wochenmittel Ist-Preis",
    )
    ax.set_ylabel("€/MWh")
    ax.set_title("Preis der Wochenkurve (mc_cb) bei fester Residuallast")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, ncol=4)
    fig.tight_layout()
    fig.savefig(GRA / "preis_bei_residuallast.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    main()
