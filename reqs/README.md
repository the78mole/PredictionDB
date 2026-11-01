# Anforderungen

Die Anforderungen an PredictionDB liegen hier als [StrictDoc](https://strictdoc.readthedocs.io/)-Projekt.
Die Gliederung folgt ISO/IEC/IEEE 29148 (Anforderungsdokumente), 15288 (System) und 12207
(Software). Begründungen, Diagramme und Alternativen bleiben in
[docs/ARCHITEKTUR.md](../docs/ARCHITEKTUR.md); die Dokumente hier verweisen im Feld `QUELLE` darauf.

## Ebenen

| Ordner | Dokument | Präfix | Inhalt | leitet sich ab aus |
|---|---|---|---|---|
| `10-strs/` | Stakeholder Requirements (StRS), mit Auftrag und Leitprinzipien (BRS) als erstem Abschnitt | `STK-` | was die Beteiligten erwarten, Randbedingungen | – |
| `20-syrs/` | System Requirements (SyRS) | `SYSR-<Bereich>-` | prüfbare Anforderungen an das ganze System einschließlich VM, Garage, PostgreSQL, Grafana, GitHub Actions und GHCR | STK |
| `30-sysa/` | Systemarchitektur (SYSA) | `SYSA-` | Systemelemente, Softwareelemente, externe Systeme und Schnittstellen | SYSR (Zuordnung) |
| `40-srs/` | Software Requirements (SRS), ein Dokument je Softwareelement | `SWR-<Dienst>-` | Anforderungen an einen Dienst bzw. ein Paket | SYSR |
| `50-swa/` | Softwarearchitektur (SWA), ein Dokument je Softwareelement | `SWA-<Dienst>-` | Komponenten (Module) und interne Schnittstellen | SWR (Zuordnung) |

Darunter liegt der Code. Nichtfunktionale Anforderungen sind keine eigene Ebene, sondern
Anforderungen der Art `Qualität` auf SYSR- bzw. SWR-Ebene. Schnittstellen stehen als Elemente
der Art `Schnittstelle` in SYSA (extern) bzw. SWA (intern).

Kürzel der Softwareelemente: `WXI` wx-ingest, `WXP` wx-process, `WXX` wx-exchange, `ENI`
energy-ingest, `MAI` mastr-ingest, `MLT` ml-train, `PRD` pdb-predict, `CORE` core, `GRIB`
gribkit. Ausgearbeitet sind alle Softwareelemente.

## Felder

Anforderungen (`grammatik/anforderung.sgra`):

- `ART`: `Funktional`, `Qualität`, `Schnittstelle` oder `Randbedingung`
- `STATUS`: Umsetzungsstand `Umgesetzt`, `Teilweise`, `Geplant`, `Vorgemerkt` oder `Verworfen`
- `NACHWEIS`: `Test`, `Analyse`, `Inspektion`, `Demonstration` (mehrere mit Komma)
- `STATEMENT` mit „muss“ (verbindlich) bzw. „soll“ (erwünscht), optional `RATIONALE`, `QUELLE`
- Ableitung über `RELATIONS` vom Typ `Parent`

Architekturelemente (`grammatik/architektur.sgra`) haben statt `NACHWEIS` das Feld `ORT` (Pfad im
Repo) und die Arten `Systemelement`, `Externes System`, `Softwareelement`, `Komponente`,
`Schnittstelle`.

## Verweise aus Code und Tests

Ein Modul verweist im Modul-Docstring auf seine SWA-Komponente, eine Funktion oder Klasse im
Docstring auf die SWR, die sie umsetzt. Tests verweisen ebenso auf die SWR, die sie prüfen:

```python
"""Ablage zugeschnittener GRIB2-Schritte in Garage (Bronze).

@relation(SWA-WXI-BRONZE, scope=file)
"""


async def put_step(...) -> StepManifest:
    """@relation(SWR-WXI-020, scope=function)"""
```

Mehrere IDs werden mit Komma getrennt: `@relation(SWR-WXI-010, SWR-WXI-022, scope=function)`.
Für Methoden gilt derselbe Docstring-Marker, `scope=class` für ganze Klassen.

## Prüfen und ansehen

```sh
uv run strictdoc export reqs --output-dir build/reqs   # HTML unter build/reqs/html/index.html
uv run strictdoc server reqs                           # Bearbeiten im Browser
uv run python reqs/pruefen.py                          # Abdeckung der SWR durch Code und Tests
```

Die veröffentlichte Fassung mit Traceability-Matrix, Abdeckung und Quelltext-Verweisen steht unter
<https://the78mole.github.io/PredictionDB/>; der Workflow `reqs-pages.yml` erneuert sie nach
jedem Push auf `main`, der `reqs/` oder Python-Code ändert.

Beides läuft als pre-commit-Hook und damit im CI-Job „Lint und Typen“. Der Export bricht ab, wenn
eine Relation oder ein `@relation`-Marker auf eine unbekannte ID zeigt. `pruefen.py` meldet jede
umgesetzte SWR ohne Verweis aus dem Code und jede mit Nachweis `Test` ohne Verweis aus einem
Test. Das HTML hängt als Artefakt `anforderungen-html` am CI-Lauf.
