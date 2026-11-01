"""Prüft die Ordner unter docs/analysen/ gegen die Konventionen aus CLAUDE.md.

Je Analyse-Ordner müssen vorhanden sein:

- ``README.md`` mit Titel (``# ...``) und dem Abschnitt ``## Ergebnis in Kürze``,
- ``job.yaml`` mit den Pflichtfeldern aus docs/ARCHITEKTUR.md 7.9, ``id`` gleich Ordnername,
- ``tabellen/lauf.json`` (außer bei den Altanalysen in ``OHNE_LAUF``, die vor dieser Prüfung
  entstanden sind und nur durch einen neuen Lauf eine ``lauf.json`` bekommen könnten).

Aufruf: ``uv run --no-sync python scripts/analysen-pruefen.py`` (auch als pre-commit-Hook).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

WURZEL = Path(__file__).resolve().parent.parent / "docs" / "analysen"

PFLICHT = (
    "id",
    "title",
    "question",
    "requested_by",
    "kind",
    "inputs",
    "method",
    "outputs",
    "schedule",
)
KIND = {"verification", "comparison", "backtest", "data_quality", "custom"}
SCHEDULE = {"once", "monthly", "on_new_model_version"}
SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Altanalysen ohne lauf.json (Stand 2026-11-01). Neue Analysen schreiben sie immer.
OHNE_LAUF = {
    "batteriespeicher-preiswirkung",
    "cloud-gpu-training",
    "eua-rueckrechnung",
    "intraday-schaetzung",
    "merit-order",
    "preisabweichung-2026-10-29",
    "prognose-de-lu-v1",
    "prognose-de-lu-v2",
    "pv-ausrichtung-speicher",
    "pv-speicher-arbitrage",
    "pv-speicher-szenario-2027",
    "zeitumstellung",
}


def pruefe(ordner: Path) -> list[str]:
    name = ordner.name
    fehler: list[str] = []
    if not SLUG.match(name):
        fehler.append("Ordnername ist kein Slug (klein, Ziffern, Bindestriche)")

    readme = ordner / "README.md"
    if not readme.is_file():
        fehler.append("README.md fehlt")
    else:
        text = readme.read_text(encoding="utf-8")
        if not text.startswith("# "):
            fehler.append("README.md beginnt nicht mit einem Titel '# ...'")
        if not re.search(r"(?m)^## Ergebnis in Kürze\s*$", text):
            fehler.append("README.md ohne Abschnitt '## Ergebnis in Kürze'")

    job = ordner / "job.yaml"
    if not job.is_file():
        fehler.append("job.yaml fehlt")
    else:
        try:
            daten = yaml.safe_load(job.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            fehler.append(f"job.yaml ist kein gültiges YAML: {exc}")
            daten = None
        if not isinstance(daten, dict):
            if daten is not None:
                fehler.append("job.yaml ist kein Mapping")
        else:
            fehlend = [k for k in PFLICHT if not daten.get(k)]
            if fehlend:
                fehler.append(f"job.yaml ohne Pflichtfelder: {', '.join(fehlend)}")
            if daten.get("id") and daten["id"] != name:
                fehler.append(f"job.yaml: id '{daten['id']}' ungleich Ordnername")
            if daten.get("kind") and daten["kind"] not in KIND:
                fehler.append(f"job.yaml: kind '{daten['kind']}' nicht in {sorted(KIND)}")
            if daten.get("schedule") and daten["schedule"] not in SCHEDULE:
                fehler.append(
                    f"job.yaml: schedule '{daten['schedule']}' nicht in {sorted(SCHEDULE)}"
                )

    lauf = ordner / "tabellen" / "lauf.json"
    if lauf.is_file():
        try:
            json.loads(lauf.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fehler.append(f"tabellen/lauf.json ist kein gültiges JSON: {exc}")
    elif name not in OHNE_LAUF:
        fehler.append("tabellen/lauf.json fehlt")
    return fehler


def main() -> int:
    ordner = sorted(p for p in WURZEL.iterdir() if p.is_dir())
    gesamt = 0
    for o in ordner:
        for f in pruefe(o):
            print(f"docs/analysen/{o.name}: {f}")
            gesamt += 1
    veraltet = sorted(n for n in OHNE_LAUF if (WURZEL / n / "tabellen" / "lauf.json").is_file())
    for n in veraltet:
        print(f"docs/analysen/{n}: hat jetzt lauf.json, bitte aus OHNE_LAUF streichen")
        gesamt += 1
    if gesamt:
        print(f"{gesamt} Verstöße gegen die Analyse-Konventionen (siehe CLAUDE.md)")
        return 1
    print(f"{len(ordner)} Analysen geprüft, alle entsprechen den Konventionen")
    return 0


if __name__ == "__main__":
    sys.exit(main())
