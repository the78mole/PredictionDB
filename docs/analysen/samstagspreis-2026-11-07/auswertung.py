"""Samstagspreis 07.11.2026: Warum ist der Day-Ahead-Preis trotz niedriger Wochenendlast hoch?

Aufruf (aus der Repo-Wurzel)::

    uv run --no-project --with pandas --with matplotlib --with httpx --with openpyxl \\
        --with pyyaml python docs/analysen/samstagspreis-2026-11-07/auswertung.py \\
        --cache /tmp/samstag

Liest den Auftrag aus ``job.yaml``, holt die freien Reihen über ``daten.py`` (Cache), den
Modellexport aus ``daten/vm_export.txt`` und schreibt ``tabellen/`` und ``grafiken/``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import yaml
from daten import (
    ec_handel_de,
    ec_kernkraft_fr,
    ec_preis,
    gas_europa,
    smard_reihe,
    usd_je_eur,
)

ORDNER = Path(__file__).resolve().parent
TAB = ORDNER / "tabellen"
GRA = ORDNER / "grafiken"
MMBTU_MWH = 0.29307
CHAMPION_ROLLE = "champion"
WOCHENTAGE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def vm_abschnitte(datei: Path) -> dict[int, pd.DataFrame]:
    """Zerlegt den psql-Export (Abschnitte ``=== n Titel``, Felder mit ``;``)."""
    teile: dict[int, list[str]] = {}
    nr = 0
    for zeile in datei.read_text().splitlines():
        if zeile.startswith("=== "):
            nr = int(zeile.split()[1])
            teile[nr] = []
        elif zeile.strip() and nr:
            teile[nr].append(zeile)
    return {n: pd.read_csv(io.StringIO("\n".join(z)), sep=";") for n, z in teile.items()}


def tagesmittel(df: pd.DataFrame) -> pd.DataFrame:
    t = df.groupby(df.index.date).mean()
    t.index = pd.to_datetime(t.index)
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("/tmp/samstagspreis"))
    args = ap.parse_args()
    cache: Path = args.cache
    cache.mkdir(parents=True, exist_ok=True)
    gestartet = datetime.now(UTC)
    job_datei = ORDNER / "job.yaml"
    job: dict[str, Any] = yaml.safe_load(job_datei.read_text())
    inp = job["inputs"]
    tag = pd.Timestamp(inp["liefertag"])
    vergleich_von = pd.Timestamp(inp["vergleich_von"])
    TAB.mkdir(exist_ok=True)
    GRA.mkdir(exist_ok=True)

    # ---- SMARD Stundenwerte -------------------------------------------------------------
    sm = inp["smard"]
    reihen = {}
    for name, ref in sm["reihen"].items():
        fid, region = str(ref).split("/")
        reihen[name] = smard_reihe(
            int(fid), region, str(sm["zeitraum"]["von"]), str(sm["zeitraum"]["bis"]), cache
        )
    h = pd.DataFrame(reihen)
    for c in h.columns:
        if c != "preis":
            h[c] = h[c] / 1000.0  # MWh je Stunde -> GW
    h["wind_prognose"] = h.wind_onshore_prognose + h.wind_offshore_prognose
    h["ee_prognose"] = h.wind_prognose + h.pv_prognose
    h["wochenende"] = h.index.dayofweek >= 5

    # ---- Tageswerte ---------------------------------------------------------------------
    spalten = [
        "preis",
        "last_prognose",
        "wind_prognose",
        "pv_prognose",
        "residuallast_prognose",
        "last",
        "residuallast",
    ]
    tage = tagesmittel(h[spalten])
    tage["wochentag"] = [WOCHENTAGE[d.weekday()] for d in tage.index]
    tage["preis_max"] = h.preis.groupby(h.index.date).max().to_numpy()
    tage["preis_min"] = h.preis.groupby(h.index.date).min().to_numpy()
    fenster = tage.loc[vergleich_von:tag].copy()
    fenster.round(2).to_csv(TAB / "tage.csv", index_label="datum")

    # ---- Zerlegung Samstag gegen Freitag und Di-Do --------------------------------------
    sa = tage.loc[tag]
    fr = tage.loc[tag - pd.Timedelta(days=1)]
    di_do = tage.loc[tag - pd.Timedelta(days=4) : tag - pd.Timedelta(days=2)].mean(
        numeric_only=True
    )
    zer = []
    for ref_name, ref in (("Freitag", fr), ("Di-Do", di_do)):
        zer.append(
            {
                "vergleich": ref_name,
                "d_last_prognose_gw": sa.last_prognose - ref.last_prognose,
                "d_wind_prognose_gw": sa.wind_prognose - ref.wind_prognose,
                "d_pv_prognose_gw": sa.pv_prognose - ref.pv_prognose,
                "d_residuallast_prognose_gw": sa.residuallast_prognose - ref.residuallast_prognose,
                "rest_gw": (sa.residuallast_prognose - ref.residuallast_prognose)
                - (sa.last_prognose - ref.last_prognose)
                + (sa.wind_prognose - ref.wind_prognose)
                + (sa.pv_prognose - ref.pv_prognose),
                "d_preis_eur_mwh": sa.preis - ref.preis,
            }
        )
    zerlegung = pd.DataFrame(zer).round(2)
    zerlegung.to_csv(TAB / "zerlegung.csv", index=False)

    # ---- Samstage seit Herbst 2025 ------------------------------------------------------
    samstage = tage[tage.index.dayofweek == 5][
        ["preis", "last_prognose", "wind_prognose", "pv_prognose", "residuallast_prognose"]
    ]
    samstage.round(2).to_csv(TAB / "samstage.csv", index_label="datum")

    # ---- Preis je Residuallastklasse ----------------------------------------------------
    h["klasse_gw"] = (h.residuallast_prognose // 5 * 5).astype("Int64")
    zeitraeume = {
        "nov_2025": (pd.Timestamp("2025-11-01"), pd.Timestamp("2025-12-01")),
        "seit_28_10_2026": (pd.Timestamp("2026-10-28"), tag),
    }
    pjr = []
    for zname, (a, b) in zeitraeume.items():
        teil = h[(h.index >= a.tz_localize(h.index.tz)) & (h.index < b.tz_localize(h.index.tz))]
        for we, g in teil.groupby("wochenende"):
            for k, gg in g.groupby("klasse_gw"):
                pjr.append(
                    {
                        "zeitraum": zname,
                        "tagtyp": "Wochenende" if we else "Werktag",
                        "residuallast_klasse_gw": int(k),
                        "stunden": len(gg),
                        "preis_median": gg.preis.median(),
                    }
                )
    preis_je_rl = pd.DataFrame(pjr).round(1)
    preis_je_rl.to_csv(TAB / "preis_je_residuallast.csv", index=False)

    # ---- Modellprognosen aus dem VM-Export ----------------------------------------------
    vm_datei = ORDNER / inp["vm_export"]["datei"]
    vm = vm_abschnitte(vm_datei)
    versionen = vm[1]
    champion = str(versionen.loc[versionen.role == CHAMPION_ROLLE, "version"].iloc[0])
    p = vm[2]
    p["stunde"] = p.h.astype(int)
    breit = p.pivot(index="stunde", columns="model_version", values="p50")
    st = h.loc[str(tag.date())].copy()
    st.index = st.index.hour
    stunden = pd.DataFrame(
        {
            "preis": st.preis,
            "last_prognose_gw": st.last_prognose,
            "wind_prognose_gw": st.wind_prognose,
            "pv_prognose_gw": st.pv_prognose,
            "residuallast_prognose_gw": st.residuallast_prognose,
        }
    )
    stunden = stunden.join(breit.add_prefix("p50_"))
    stunden.round(2).to_csv(TAB / "stunden_07_11.csv", index_label="stunde")

    mt = vm[3]
    mt["d"] = pd.to_datetime(mt.d)
    mt = mt.merge(tage[["preis"]], left_on="d", right_index=True)
    mt["fehler"] = mt.p50 - mt.preis
    mt["rolle"] = mt.model_version.map(dict(zip(versionen.version, versionen.role, strict=True)))
    mt.round({"p50": 1, "preis": 1, "fehler": 1}).to_csv(
        TAB / "modelle_tagesmittel.csv", index=False
    )
    mae_stunde = {
        v: float((stunden[f"p50_{v}"] - stunden.preis).abs().mean()) for v in breit.columns
    }

    # ---- Nachbarzonen, Handel, Kernkraft FR ---------------------------------------------
    ec = inp["energy_charts"]
    pv_, pb = str(ec["preise_zeitraum"]["von"]), str(ec["preise_zeitraum"]["bis"])
    lizenzen = {}
    nach = {}
    for zone in ["DE-LU", *ec["preise_nachbarn"]]:
        s, lic = ec_preis(zone, pv_, pb, cache)
        if "CC BY 4.0" not in lic:
            print(f"{zone}: Lizenz {lic!r}, übersprungen")
            continue
        lizenzen[zone] = lic
        nach[zone] = s.groupby(s.index.date).mean()
    nachbar = pd.DataFrame(nach)
    nachbar.index = pd.to_datetime(nachbar.index)
    iv, ib = str(ec["zeitraum_ist"]["von"]), str(ec["zeitraum_ist"]["bis"])
    handel = ec_handel_de(iv, ib, cache)
    kkw = ec_kernkraft_fr(iv, ib, cache)
    handel_t = handel.groupby(handel.index.date).mean()
    kkw_t = kkw.groupby(kkw.index.date).mean() / 1000.0
    handel_t.index = pd.to_datetime(handel_t.index)
    kkw_t.index = pd.to_datetime(kkw_t.index)
    nachbar["handel_de_gw"] = handel_t
    nachbar["kernkraft_fr_gw"] = kkw_t
    nachbar.round(2).to_csv(TAB / "nachbarpreise.csv", index_label="datum")
    np_tag = nachbar.loc[tag].drop(["handel_de_gw", "kernkraft_fr_gw"]).dropna()

    # ---- Gas gegen Strom je Monat -------------------------------------------------------
    gas = gas_europa(cache)
    fx = usd_je_eur(cache)
    wp = inp["weltbank_pink_sheet"]["zeitraum"]
    monate = pd.period_range(str(wp["von"]), str(wp["bis"]), freq="M")
    fx_m = fx.reindex(monate).ffill()
    gas_eur = gas.reindex(monate) / fx_m / MMBTU_MWH
    preis_lang = smard_reihe(4169, "DE-LU", f"{wp['von']}-01", str(sm["zeitraum"]["bis"]), cache)
    strom_m = preis_lang.groupby(preis_lang.index.tz_localize(None).to_period("M")).mean()
    gsm = pd.DataFrame(
        {
            "gas_usd_mmbtu": gas.reindex(monate),
            "usd_je_eur": fx_m,
            "gas_eur_mwh_th": gas_eur,
            "gud_brennstoff_eur_mwh": gas_eur / 0.55,
            "gasturbine_brennstoff_eur_mwh": gas_eur / 0.38,
            "strom_de_lu_eur_mwh": strom_m.reindex(monate),
        }
    )
    gsm.round(2).to_csv(TAB / "gas_strom_monat.csv", index_label="monat")

    # ---- Zusammenfassung ----------------------------------------------------------------
    ref_nov = h[(h.index >= "2025-11-01") & (h.index < "2025-12-01")]
    nahe = ref_nov[(ref_nov.residuallast_prognose - sa.residuallast_prognose).abs() <= 2.5]
    zus = {
        "preis_tagesmittel_07_11": sa.preis,
        "preis_max_07_11": sa.preis_max,
        "preis_min_07_11": sa.preis_min,
        "preis_freitag_06_11": fr.preis,
        "preis_samstag_24_10": tage.loc["2026-10-24", "preis"],
        "last_prognose_07_11_gw": sa.last_prognose,
        "last_prognose_freitag_gw": fr.last_prognose,
        "wind_prognose_07_11_gw": sa.wind_prognose,
        "wind_prognose_freitag_gw": fr.wind_prognose,
        "wind_prognose_samstag_24_10_gw": tage.loc["2026-10-24", "wind_prognose"],
        "pv_prognose_07_11_gw": sa.pv_prognose,
        "residuallast_prognose_07_11_gw": sa.residuallast_prognose,
        "residuallast_prognose_freitag_gw": fr.residuallast_prognose,
        "residuallast_prognose_di_do_gw": di_do.residuallast_prognose,
        "preis_nov_2025_bei_gleicher_residuallast_median": nahe.preis.median(),
        "stunden_nov_2025_bei_gleicher_residuallast": len(nahe),
        "gas_eur_mwh_th_2025_11": gsm.loc[pd.Period("2025-11", "M"), "gas_eur_mwh_th"],
        "gas_eur_mwh_th_2026_10": gsm.loc[pd.Period("2026-10", "M"), "gas_eur_mwh_th"],
        "nachbarn_tagesmittel_min": np_tag.drop("DE-LU").min(),
        "nachbarn_tagesmittel_max": np_tag.drop("DE-LU").max(),
        "nachbarn_innerhalb_10_eur": int(((np_tag.drop("DE-LU") - sa.preis).abs() <= 10).sum()),
        "nachbarn_anzahl": len(np_tag) - 1,
        "champion": champion,
        "champion_p50_tagesmittel": float(breit[champion].mean()),
        "champion_mae_stunde": mae_stunde[champion],
        "bestes_modell": min(mae_stunde, key=lambda k: mae_stunde[k]),
        "bestes_modell_p50_tagesmittel": float(
            breit[min(mae_stunde, key=lambda k: mae_stunde[k])].mean()
        ),
        "bestes_modell_mae_stunde": min(mae_stunde.values()),
    }
    pd.Series(zus).to_frame("wert").to_csv(TAB / "zusammenfassung.csv", index_label="kennzahl")

    # ---- Grafiken -----------------------------------------------------------------------
    plt.rcParams.update({"font.size": 9})
    fig, ax = plt.subplots(figsize=(9, 4.8))
    x = range(len(fenster))
    ax.bar(x, fenster.last_prognose, color="#c9cdd6", label="Lastprognose")
    ax.bar(
        x,
        fenster.wind_prognose + fenster.pv_prognose,
        color="#5aa469",
        label="Wind + PV (Prognose)",
    )
    ax.plot(
        x, fenster.residuallast_prognose, "o-", color="#33415c", label="Residuallast (Prognose)"
    )
    ax.set_ylabel("GW (Tagesmittel)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(
        [f"{WOCHENTAGE[d.weekday()]} {d:%d.%m.}" for d in fenster.index], rotation=60, ha="right"
    )
    ax2 = ax.twinx()
    ax2.plot(x, fenster.preis, "s--", color="#d1495b", label="Day-Ahead-Preis")
    ax2.set_ylabel("EUR/MWh")
    ax2.set_ylim(bottom=0)
    for i, d in enumerate(fenster.index):
        if d.weekday() >= 5:
            ax.axvspan(i - 0.5, i + 0.5, color="#f2e8cf", zorder=-1)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper center", bbox_to_anchor=(0.5, -0.28), fontsize=8, ncol=4)
    ax.set_title("Tagesmittel 24.10.-07.11.2026 (Wochenenden hinterlegt)")
    fig.tight_layout()
    fig.savefig(GRA / "tage_residuallast_preis.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    for zname, (a, b), farbe in (
        ("Nov. 2025", zeitraeume["nov_2025"], "#8d99ae"),
        ("seit 28.10.2026", zeitraeume["seit_28_10_2026"], "#d1495b"),
    ):
        teil = h[(h.index >= a.tz_localize(h.index.tz)) & (h.index < b.tz_localize(h.index.tz))]
        for we, mk in ((False, "o"), (True, "^")):
            g = teil[teil.wochenende == we]
            ax.scatter(
                g.residuallast_prognose,
                g.preis,
                s=10,
                alpha=0.45,
                marker=mk,
                color=farbe,
                label=f"{zname}, {'Wochenende' if we else 'Werktag'}",
            )
    ax.scatter(
        st.residuallast_prognose, st.preis, s=28, color="black", label="07.11.2026 (Samstag)"
    )
    ax.set_xlabel("Residuallastprognose DE (GW)")
    ax.set_ylabel("Day-Ahead-Preis DE-LU (EUR/MWh)")
    ax.legend(fontsize=7)
    ax.set_title(
        "Preis je Residuallast: gleiche Kurve für Werktag und Wochenende, Niveau verschoben"
    )
    fig.tight_layout()
    fig.savefig(GRA / "preis_je_residuallast.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(stunden.index, stunden.preis, "k-", lw=2.2, label="Ist")
    farben = ["#8d99ae", "#2b2d42", "#edae49", "#00798c", "#66a182", "#d1495b"]
    for v, f in zip(breit.columns, farben, strict=False):
        rolle = versionen.set_index("version").role.get(v, "")
        ax.plot(stunden.index, stunden[f"p50_{v}"], color=f, lw=1.2, label=f"P50 {v} ({rolle})")
    ax.set_xlabel("Stunde (MEZ)")
    ax.set_ylabel("EUR/MWh")
    ax3 = ax.twinx()
    ax3.fill_between(stunden.index, stunden.residuallast_prognose_gw, color="#33415c", alpha=0.12)
    ax3.set_ylabel("Residuallastprognose (GW, Fläche)")
    ax3.set_ylim(0, 80)
    ax.legend(fontsize=6.5, loc="upper left")
    ax.set_title("07.11.2026: Day-Ahead-Preis und Modellprognosen (Ausgabe 06.11. 12 Uhr)")
    fig.tight_layout()
    fig.savefig(GRA / "stunden_07_11.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 4))
    xs = gsm.index.to_timestamp()
    ax.plot(
        xs, gsm.strom_de_lu_eur_mwh, "-o", ms=3, color="#d1495b", label="Strom DE-LU (Monatsmittel)"
    )
    ax.plot(
        xs,
        gsm.gud_brennstoff_eur_mwh,
        "-",
        color="#33415c",
        label="Gas Europa / 0,55 (GuD, ohne CO2)",
    )
    ax.plot(xs, gsm.gas_eur_mwh_th, ":", color="#33415c", label="Gas Europa (EUR/MWh thermisch)")
    ax.set_ylabel("EUR/MWh")
    ax.legend(fontsize=8)
    ax.set_title("Strompreis folgt dem Gaspreis (Weltbank, Monatsmittel)")
    fig.tight_layout()
    fig.savefig(GRA / "gas_strom_monat.png", dpi=130)
    plt.close(fig)

    # ---- Laufprotokoll ------------------------------------------------------------------
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, cwd=ORDNER
    ).stdout.strip()
    lauf = {
        "job": job["id"],
        "job_sha256": sha(job_datei),
        "git_commit": commit,
        "gestartet": gestartet.isoformat(timespec="seconds"),
        "beendet": datetime.now(UTC).isoformat(timespec="seconds"),
        "zeitraum_smard": [str(h.index.min()), str(h.index.max())],
        "stunden_smard": len(h),
        "fehlende_preisstunden": int(h.preis.isna().sum()),
        "zeitraum_nachbarn": [pv_, pb],
        "zonen_nachbarn": list(lizenzen),
        "zeitraum_handel_kernkraft_fr": [iv, ib],
        "monate_gas": [str(monate[0]), str(monate[-1])],
        "monate_ohne_wechselkurs_fortgeschrieben": [
            str(m) for m in monate if pd.isna(fx.reindex(monate).loc[m])
        ],
        "vm_export_sha256": sha(vm_datei),
        "modellversionen": list(breit.columns),
        "champion": champion,
        "annahmen": [
            "Residuallast und Erzeugung als ÜNB-Day-Ahead-Prognose (SMARD), weil der Preis am "
            "Vortag auf Basis von Prognosen entsteht und Ist-Werte für den 07.11. noch fehlen",
            "Grenzkosten nur Brennstoff, ohne CO2 und ohne Fixkosten",
        ],
    }
    (TAB / "lauf.json").write_text(json.dumps(lauf, indent=1, ensure_ascii=False) + "\n")
    print(json.dumps(zus, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
