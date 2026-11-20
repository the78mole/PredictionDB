"""Kachel „Modellvergleich“ der Landing Page aus dem Backtest-Bericht in GHCR.

Der Workflow „Backtest und Training“ legt die Kennzahlen jedes Backtests als
``ghcr.io/the78mole/predictiondb-models/de_lu:backtest-<konfiguration>`` ab, mit den Rollen der
Registry zur Zeit des Laufs (``rollen.json``). Hier wird daraus eine Tabelle: Champion,
Challenger, Vorgänger und Baseline gegen die naiven Vergleichsverfahren, MAE je Ziel und
Vorlauftag im festen Vergleichsfenster.
"""

from __future__ import annotations

import html
import io
import json
import os
from typing import Any

import pandas as pd

KONFIG = os.environ.get("PDB_PAGES_MODEL_CONFIG", "de_lu_v2")
#: Spalten: Ziel, Vorlauftag, Überschrift, Teiler (Last MW → GW)
SPALTEN = (
    ("da_price_de_lu", 1, "Preis D+1", 1.0),
    ("da_price_de_lu", 2, "Preis D+2", 1.0),
    ("da_price_de_lu", 7, "Preis D+7", 1.0),
    ("load_actual", 1, "Last D+1", 1000.0),
    ("load_actual", 2, "Last D+2", 1000.0),
)
ROLLEN = {
    "champion": "Champion",
    "challenger": "Challenger",
    "vorgaenger": "Vorgänger",
    "baseline": "Baseline",
}
#: Vergleichsverfahren ohne Modell, die immer mit in die Tabelle kommen
NAIV = {"naive_day": "Vortag (naiv)", "naive_week": "Vorwoche (naiv)"}
REIHENFOLGE = ("champion", "challenger", "vorgaenger", "baseline")
#: Fehlerkurven je Zeile: Ziel, Überschrift, Teiler, Einheit
KURVEN = (
    ("da_price_de_lu", "Preis", 1.0, "€/MWh"),
    ("load_actual", "Last", 1000.0, "GW"),
)
#: Vorlauftage der Kurven
TAGE = tuple(range(1, 16))
#: Vergleichslinie in der großen Ansicht
REFERENZ = "naive_week"


def bericht_holen() -> dict[str, bytes] | None:
    """Bericht aus GHCR, anonym oder mit ``GH_TOKEN``; ``None`` bei jedem Fehler."""
    try:
        import httpx

        from ml_train import model_ghcr

        os.environ.setdefault("WX_EXCHANGE_TOKEN", os.environ.get("GH_TOKEN", ""))
        os.environ.setdefault("WX_EXCHANGE_USERNAME", os.environ.get("GITHUB_ACTOR", "token"))
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0)) as client:
            return model_ghcr.pull_report(model_ghcr.connect(client, read_only=True), KONFIG)
    except Exception as e:  # die Seite baut auch ohne Bericht
        print(f"Modellvergleich: kein Bericht ({type(e).__name__}: {e})")
        return None


def _zahl(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _punkte(
    werte: dict[int, float], x0: float, x1: float, y0: float, y1: float, lo: float, hi: float
) -> str:
    """SVG-Koordinaten der Kurve; x über die Vorlauftage, y von ``lo`` (unten) bis ``hi``."""
    span = (hi - lo) or 1.0
    return " ".join(
        f"{x0 + (x1 - x0) * (t - TAGE[0]) / (TAGE[-1] - TAGE[0]):.1f},"
        f"{y1 - (y1 - y0) * (v - lo) / span:.1f}"
        for t, v in sorted(werte.items())
    )


def kurve(werte: dict[int, float], ref: dict[int, float], titel: str, einheit: str) -> str:
    """Miniatur der Fehlerkurve D+1 bis D+15, beim Darüberfahren groß mit Achsen und Vorwoche.

    Jede Kurve hat ihre eigene Skala, damit der Anstieg über den Vorlauf sichtbar bleibt; die
    Zahlen stehen in den Spalten davor und an der Achse der großen Ansicht.
    """
    if len(werte) < 2:
        return "&ndash;"
    beschr = f"{titel}: MAE D+{min(werte)} bis D+{max(werte)} in {einheit}"
    linie = _punkte(werte, 2, 94, 3, 25, min(werte.values()), max(werte.values()))
    mini = (
        f'<svg class="mini" viewBox="0 0 96 28" width="96" height="28" aria-hidden="true">'
        f'<polyline points="{linie}"/></svg>'
    )
    # große Ansicht: Achsen links und unten, Vorwoche gestrichelt, Werte als Tooltip je Punkt
    alle = [*werte.values(), *ref.values()]
    lo, hi = min(alle), max(alle)
    rand = (hi - lo) * 0.08 or 1.0
    lo, hi = max(0.0, lo - rand), hi + rand
    x0, x1, y0, y1 = 44.0, 296.0, 14.0, 150.0
    achsen = []
    for i in range(3):
        v = lo + (hi - lo) * i / 2
        y = y1 - (y1 - y0) * i / 2
        achsen.append(
            f'<line class="raster" x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}"/>'
            f'<text x="{x0 - 6}" y="{y + 4:.1f}" text-anchor="end">{_zahl(v)}</text>'
        )
    for t in (1, 5, 10, 15):
        x = x0 + (x1 - x0) * (t - TAGE[0]) / (TAGE[-1] - TAGE[0])
        achsen.append(f'<text x="{x:.1f}" y="{y1 + 18}" text-anchor="middle">D+{t}</text>')
    ref_linie = ""
    if len(ref) >= 2:
        ref_linie = f'<polyline class="ref" points="{_punkte(ref, x0, x1, y0, y1, lo, hi)}"/>'
    punkte = []
    for t, v in sorted(werte.items()):
        xy = _punkte({t: v}, x0, x1, y0, y1, lo, hi).split(",")
        punkte.append(
            f'<circle cx="{xy[0]}" cy="{xy[1]}" r="3"><title>D+{t}: {_zahl(v)} {einheit}</title>'
            "</circle>"
        )
    gross = (
        f'<span class="gross" role="img" aria-label="{html.escape(beschr)}">'
        f'<span class="gross-titel">{html.escape(titel)}, MAE in {html.escape(einheit)}'
        f"{' · gestrichelt: Vorwoche (naiv)' if ref_linie else ''}</span>"
        f'<svg viewBox="0 0 320 172" width="320" height="172">{"".join(achsen)}{ref_linie}'
        f'<polyline points="{_punkte(werte, x0, x1, y0, y1, lo, hi)}"/>{"".join(punkte)}'
        "</svg></span>"
    )
    return f'<span class="kurve" tabindex="0" title="{html.escape(beschr)}">{mini}{gross}</span>'


def tabelle(files: dict[str, bytes]) -> tuple[str, str] | None:
    """HTML-Tabelle und Untertitel aus den Dateien des Berichts."""
    lauf: dict[str, Any] = json.loads(files.get("lauf.json", b"{}"))
    rollen = json.loads(files.get("rollen.json", b"{}")).get("roles", {})
    fenster = lauf.get("compare") or {}
    name = "metrics_window_lead.csv" if "metrics_window_lead.csv" in files else "metrics_lead.csv"
    if name not in files:
        return None
    m = pd.read_csv(io.BytesIO(files[name]))
    if "window" in m and fenster.get("name"):
        m = m[m["window"] == fenster["name"]]
    if "complete" in m:  # nur Verfahren, die das ganze Fenster abdecken
        m = m[m["complete"].astype(str).str.lower() == "true"]
    m = m.drop_duplicates(["method", "target", "lead_day"])
    mae = m.set_index(["method", "target", "lead_day"])["mae"]
    zeilen: list[tuple[str, str]] = []
    for rolle in REIHENFOLGE:
        for variante, rs in rollen.items():
            if rs and rs[0] == rolle and variante in m["method"].values:
                label = " · ".join(ROLLEN[r] for r in rs)
                zeilen.append((variante, label))
    gezeigt = {v for v, _ in zeilen}
    for variante in sorted(set(m["method"]) - gezeigt - set(NAIV)):
        if not str(variante).startswith("bench_"):
            zeilen.append((variante, "im Backtest"))
    for verfahren, label in NAIV.items():
        if verfahren in m["method"].values:
            zeilen.append((verfahren, label))
    if not zeilen:
        return None
    kopf = "".join(f"<th>{html.escape(t)}</th>" for _, _, t, _ in SPALTEN)
    kopf += "".join(f"<th>{html.escape(t)} D+1 bis D+15</th>" for _, t, _, _ in KURVEN)

    def reihe(verfahren: str, ziel: str, teiler: float) -> dict[int, float]:
        out = {}
        for t in TAGE:
            v = mae.get((verfahren, ziel, t))
            if v is not None and pd.notna(v):
                out[t] = float(v) / teiler
        return out

    body = []
    for verfahren, label in zeilen:
        zellen = []
        for ziel, tag, _, teiler in SPALTEN:
            v = mae.get((verfahren, ziel, tag))
            text = _zahl(float(v) / teiler) if v is not None and pd.notna(v) else "&ndash;"
            zellen.append(f"<td>{text}</td>")
        for ziel, titel, teiler, einheit in KURVEN:
            ref = reihe(REFERENZ, ziel, teiler) if verfahren != REFERENZ else {}
            bild = kurve(reihe(verfahren, ziel, teiler), ref, titel, einheit)
            zellen.append(f'<td class="kurven">{bild}</td>')
        body.append(
            f'<tr><th scope="row">{html.escape(str(verfahren))}'
            f'<span class="rolle">{html.escape(label)}</span></th>{"".join(zellen)}</tr>'
        )
    if fenster.get("name"):
        zeitraum = f"Vergleichsfenster {fenster['start']} bis {fenster['end']}"
    else:
        zeitraum = "alle Testtage"
    unter = (
        f"MAE im Backtest, {zeitraum}; Preis in €/MWh, Last in GW. "
        f"Snapshot {lauf.get('snapshot_id', '?')}, Stand {str(lauf.get('finished', '?'))[:10]}."
    )
    tab = (
        f'<table class="modelle"><thead><tr><th>Verfahren</th>{kopf}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>"
    )
    return tab, unter


def kachel(files: dict[str, bytes] | None) -> str:
    """Kachel über die volle Breite; ohne Bericht ein Hinweis."""
    inhalt = tabelle(files) if files else None
    if inhalt is None:
        return (
            '<section class="card intro"><h2>Modellvergleich</h2>'
            '<p class="stat warn">kein Bericht</p>'
            "<p>Noch kein Backtest aus GitHub Actions in GHCR.</p></section>"
        )
    tab, unter = inhalt
    return (
        f'<section class="card intro"><h2>Modellvergleich {html.escape(KONFIG)}</h2>'
        f'<p>{html.escape(unter)}</p><div class="modelle-wrap">{tab}</div>'
        '<a class="more" href="https://github.com/the78mole/PredictionDB/pkgs/container/'
        'predictiondb-models%2Fde_lu">Modelle in GHCR →</a></section>'
    )
