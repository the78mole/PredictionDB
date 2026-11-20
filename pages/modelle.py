"""Kachel „Modellvergleich“ der Landing Page aus dem Backtest-Bericht in GHCR.

Der Workflow „Backtest und Training“ legt die Kennzahlen jedes Backtests als
``ghcr.io/the78mole/predictiondb-models/de_lu:backtest-<konfiguration>`` ab, mit den Rollen der
Registry zur Zeit des Laufs (``rollen.json``). Hier wird daraus eine Tabelle: Champion,
Challenger, Vorgänger und Baseline gegen die naiven Vergleichsverfahren, MAE je Ziel und
Vorlauftag im festen Vergleichsfenster. Am Zeilenende zeigt je ein Miniatur-Plot den MAE über
die Vorlauftage D+1 bis D+15 für Preis und Last; beim Überfahren (oder Fokus) erscheint er groß
mit Achsen und dem naiven Vortag als Vergleichslinie.
"""

from __future__ import annotations

import html
import io
import json
import math
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
#: Fehlerkurven am Zeilenende: Ziel, Überschrift, Einheit, Teiler
KURVEN = (
    ("da_price_de_lu", "Preis", "€/MWh", 1.0),
    ("load_actual", "Last", "GW", 1000.0),
)
#: Vorlauftage der Fehlerkurven
KURVE_TAGE = range(1, 16)
#: Vergleichslinie im großen Plot
KURVE_REFERENZ = "naive_day"


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


def _kurve(mae: pd.Series, verfahren: str, ziel: str, teiler: float) -> list[float | None]:
    """MAE je Vorlauftag D+1 bis D+15, ``None`` wo der Bericht keinen Wert hat."""
    werte: list[float | None] = []
    for tag in KURVE_TAGE:
        v = mae.get((verfahren, ziel, tag))
        werte.append(float(v) / teiler if v is not None and pd.notna(v) else None)
    return werte


def _pfade(werte: list[float | None], x0: float, dx: float, y: Any) -> list[str]:
    """Polylinien-Punkte, an Lücken aufgetrennt."""
    pfade: list[str] = []
    punkte: list[str] = []
    for i, w in enumerate(werte):
        if w is None:
            if punkte:
                pfade.append(" ".join(punkte))
            punkte = []
            continue
        punkte.append(f"{x0 + i * dx:.1f},{y(w):.1f}")
    if punkte:
        pfade.append(" ".join(punkte))
    return pfade


def achse(werte: list[float]) -> tuple[float, float]:
    """Achsenbereich auf runde Schritte (vier Teilungen), damit die Kurven ihre Form zeigen."""
    if not werte:
        return 0.0, 1.0
    lo, hi = min(werte), max(werte)
    spanne = (hi - lo) or abs(hi) or 1.0
    roh = spanne / 4
    stufe = 10.0 ** math.floor(math.log10(roh))
    schritt = next(f * stufe for f in (1, 2, 2.5, 5, 10) if f * stufe >= roh)
    unten = math.floor(lo / schritt) * schritt
    oben = math.ceil(hi / schritt) * schritt
    while (oben - unten) / schritt > 4:
        schritt = next(f * stufe for f in (2, 2.5, 5, 10, 20) if f * stufe > schritt)
        unten = math.floor(lo / schritt) * schritt
        oben = math.ceil(hi / schritt) * schritt
    return unten, max(oben, unten + schritt)


def _anteil(v: float, bereich: tuple[float, float]) -> float:
    """Lage eines Werts im Achsenbereich, auf 0 bis 1 begrenzt."""
    lo, hi = bereich
    return min(max((v - lo) / (hi - lo), 0.0), 1.0)


def plot(
    werte: list[float | None],
    referenz: list[float | None] | None,
    bereich: tuple[float, float],
    titel: str,
    einheit: str,
) -> str:
    """Miniatur (SVG) mit großer Ansicht beim Überfahren.

    Die Miniatur nutzt ``bereich``, für alle Zeilen eines Ziels gleich, damit die Zeilen
    vergleichbar bleiben; die große Ansicht skaliert auf die eigene Kurve und die Vergleichslinie.

    @relation(SWR-MLT-083, scope=function)
    """
    if not any(w is not None for w in werte):
        return "&ndash;"
    beschreibung = ", ".join(
        f"D+{t} {_zahl(w)}" for t, w in zip(KURVE_TAGE, werte, strict=True) if w is not None
    )
    # Miniatur 96 x 28
    mw, mh, mp = 96.0, 28.0, 2.0
    n = len(werte)

    def my(v: float) -> float:
        return mh - mp - (mh - 2 * mp) * _anteil(v, bereich)

    mini = "".join(
        f'<polyline class="linie" points="{p}"/>'
        for p in _pfade(werte, mp, (mw - 2 * mp) / (n - 1), my)
    )
    # große Ansicht 340 x 200 mit Achsen
    gw, gh, links, rechts, oben, unten = 340.0, 200.0, 44.0, 10.0, 36.0, 26.0
    bw, bh = gw - links - rechts, gh - oben - unten
    dx = bw / (n - 1)

    gross = achse([w for w in [*werte, *(referenz or [])] if w is not None])

    def gy(v: float) -> float:
        return oben + bh - bh * _anteil(v, gross)

    teile = [
        f'<text class="titel" x="{links}" y="16">{html.escape(titel)} · MAE in '
        f"{html.escape(einheit)}</text>"
    ]
    for k in range(5):
        v = gross[0] + (gross[1] - gross[0]) * k / 4
        yy = gy(v)
        teile.append(
            f'<line class="gitter" x1="{links}" x2="{gw - rechts}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
            f'<text class="achse" x="{links - 6}" y="{yy + 4:.1f}" text-anchor="end">'
            f"{_zahl(v)}</text>"
        )
    for i, t in enumerate(KURVE_TAGE):
        if t in (1, 5, 10, 15):
            teile.append(
                f'<text class="achse" x="{links + i * dx:.1f}" y="{gh - 8}" '
                f'text-anchor="middle">D+{t}</text>'
            )
    if referenz is not None:
        teile += [
            f'<polyline class="referenz" points="{p}"/>' for p in _pfade(referenz, links, dx, gy)
        ]
        teile.append(
            f'<text class="achse" x="{gw - rechts}" y="30" text-anchor="end">'
            "- - Vortag (naiv)</text>"
        )
    teile += [f'<polyline class="linie" points="{p}"/>' for p in _pfade(werte, links, dx, gy)]
    teile += [
        f'<circle class="punkt" cx="{links + i * dx:.1f}" cy="{gy(w):.1f}" r="2.5">'
        f"<title>D+{t}: {_zahl(w)} {html.escape(einheit)}</title></circle>"
        for i, (t, w) in enumerate(zip(KURVE_TAGE, werte, strict=True))
        if w is not None
    ]
    return (
        f'<span class="spark" tabindex="0" aria-label="{html.escape(titel)}: {beschreibung}">'
        f'<svg class="mini" viewBox="0 0 {mw:.0f} {mh:.0f}" width="{mw:.0f}" '
        f'height="{mh:.0f}" aria-hidden="true">{mini}</svg>'
        f'<span class="gross"><svg viewBox="0 0 {gw:.0f} {gh:.0f}" width="{gw:.0f}" '
        f'height="{gh:.0f}" aria-hidden="true">{"".join(teile)}</svg></span></span>'
    )


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
    kopf += "".join(
        f'<th class="kurve">{html.escape(t)} D+1&ndash;15</th>' for _, t, _, _ in KURVEN
    )
    kurven = {
        (verfahren, ziel): _kurve(mae, verfahren, ziel, teiler)
        for verfahren, _ in zeilen
        for ziel, _, _, teiler in KURVEN
    }
    # Miniaturen: gleiche y-Achse für alle Zeilen eines Ziels, damit sie vergleichbar sind
    bereiche = {
        ziel: achse([w for (_, z), ws in kurven.items() if z == ziel for w in ws if w])
        for ziel, _, _, _ in KURVEN
    }
    body = []
    for verfahren, label in zeilen:
        zellen = []
        for ziel, tag, _, teiler in SPALTEN:
            v = mae.get((verfahren, ziel, tag))
            text = _zahl(float(v) / teiler) if v is not None and pd.notna(v) else "&ndash;"
            zellen.append(f"<td>{text}</td>")
        for ziel, titel, einheit, teiler in KURVEN:
            referenz = (
                _kurve(mae, KURVE_REFERENZ, ziel, teiler) if verfahren != KURVE_REFERENZ else None
            )
            if referenz is not None and not any(w is not None for w in referenz):
                referenz = None
            titel_plot = f"{verfahren} · {titel}"
            svg = plot(kurven[(verfahren, ziel)], referenz, bereiche[ziel], titel_plot, einheit)
            zellen.append(f'<td class="kurve">{svg}</td>')
        body.append(
            f'<tr><th scope="row">{html.escape(str(verfahren))}'
            f'<span class="rolle">{html.escape(label)}</span></th>{"".join(zellen)}</tr>'
        )
    if fenster.get("name"):
        zeitraum = f"Vergleichsfenster {fenster['start']} bis {fenster['end']}"
    else:
        zeitraum = "alle Testtage"
    unter = (
        f"MAE im Backtest, {zeitraum}; Preis in €/MWh, Last in GW. Kurven: MAE über D+1 bis "
        "D+15, gleiche Skala je Spalte, groß beim Überfahren. "
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
