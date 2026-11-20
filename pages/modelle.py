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
    body = []
    for verfahren, label in zeilen:
        zellen = []
        for ziel, tag, _, teiler in SPALTEN:
            v = mae.get((verfahren, ziel, tag))
            text = _zahl(float(v) / teiler) if v is not None and pd.notna(v) else "&ndash;"
            zellen.append(f"<td>{text}</td>")
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
