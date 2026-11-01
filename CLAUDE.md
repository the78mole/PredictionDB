# Hinweise für Claude und andere Mitwirkende

Sprache im Repo ist Deutsch (Doku, Commit-Texte nach Conventional Commits, Kommentare).

## Analysen unter `docs/analysen/`

Jede Analyse ist ein eigener Ordner `docs/analysen/<slug>/` und folgt dem Ablauf aus
[docs/ARCHITEKTUR.md, Abschnitt 7.9](docs/ARCHITEKTUR.md#79-analyse-jobs). Diese Regeln gelten
für **jede** neue Analyse, auch für schnelle einmalige Auswertungen und reine Abschätzungen.
`scripts/analysen-pruefen.py` prüft sie als pre-commit-Hook und in der CI.

### Pflichtbestandteile

| Datei | Inhalt |
|---|---|
| `job.yaml` | Der Auftrag, **vor dem Lauf** geschrieben und danach unverändert (Schema unten). |
| `README.md` | Ergebnisbericht, wird für jedes Release als PDF gerendert (`scripts/analysen-pdf.sh`). |
| `tabellen/*.csv` | Alle Zahlen, die im README stehen, maschinenlesbar. |
| `tabellen/lauf.json` | Protokoll des Laufs, vom Skript geschrieben (siehe unten). |
| `grafiken/*.png` bzw. `*.svg` | Grafiken, im README eingebunden. |
| `auswertung.py` | Eigene Logik (`kind: custom` usw.). Weitere Skripte daneben sind erlaubt (`abruf.py`, `daten.py`). |

Ältere Analysen ohne `lauf.json` stehen in `OHNE_LAUF` im Prüfskript. Die Liste wird nicht
erweitert; wer eine dieser Analysen neu laufen lässt, schreibt die `lauf.json` und streicht den
Ordner aus der Liste.

### `job.yaml`

Kopfkommentar, dann die Pflichtfelder in dieser Reihenfolge. Beispiele: `gfs-folgelauf/`,
`zeitumstellung/`, `prognose-de-lu-v2/`.

```yaml
# Auftrag nach docs/ARCHITEKTUR.md 7.9; ausgeführt von auswertung.py im selben Ordner
id: <slug>                     # gleich dem Ordnernamen
title: <kurzer Titel>
question: >
  <die Frage, so wie sie gestellt wurde>
requested_by: <wer gefragt hat>
kind: custom                   # verification | comparison | backtest | data_quality | custom
inputs:                        # Quellen, Codes, Zeiträume, Snapshot, Konfiguration
  ...
method:                        # Verfahren, Parameter, Varianten, Kennzahlen (metrics), group_by
  ...
outputs:
  tables: [...]                # Dateinamen in tabellen/ ohne Endung
  figures: [...]               # Dateinamen in grafiken/ ohne Endung
schedule: once                 # once | monthly | on_new_model_version
```

Von Hand übernommene Werte stehen unter `inputs.manual` mit Verweis auf die Datei, in der jede
Zahl ihre Quelle trägt.

### `README.md`

- Titel `# ...`, darunter ein kurzer Einstieg: „Einmalige Auswertung vom TT.MM.JJJJ. Frage: …“.
- Erster Abschnitt `## Ergebnis in Kürze` (Pflicht, wird geprüft): die Antwort zuerst.
- Danach je nach Analyse Ergebnisse, `## Daten und Methode` (oder `## Modell` / `## Daten`),
  `## Grenzen` bzw. `## Einschränkungen`, `## Quellen` mit Lizenz und Namensnennung je Quelle,
  und ein Abschnitt zum Nachrechnen (`## Nachvollziehen`, `## Wiederholen` oder `## Aufruf`)
  mit dem vollständigen Befehl, z. B.
  `uv run --no-project --with pandas ... python docs/analysen/<slug>/auswertung.py --cache /tmp/x`.
- Relative Links auf Tabellen, Grafiken und andere Analysen (`../<slug>/README.md`).

### `tabellen/lauf.json`

Schreibt das Auswertungsskript am Ende jedes Laufs. Enthält mindestens `job` (id),
`git_commit`, `gestartet`/`beendet` (UTC, ISO 8601), die tatsächlich verwendeten Zeiträume und
den Umfang (Zeilen, Stationen, Paare, Stunden). Wo es zutrifft: `job_sha256`, fehlende und
zeitlich interpolierte Schritte, Snapshot- bzw. Image-Version, Annahmen. Vorbild:
`gfs-folgelauf/auswertung.py`.

### Daten und Lizenzen

- Nur freie Daten veröffentlichen. Rohdaten unfreier oder lizenzloser Quellen bleiben lokal,
  ins Repo kommen nur aggregierte Kennzahlen (Beispiel `intraday-schaetzung/`).
- Für Analysen dürfen Werte aus PDFs oder Webseiten einmalig von Hand übernommen werden, mit
  Quelle je Wert. Für regelmäßige Datenbank-Inputs gilt das nicht.
- Große Zwischenergebnisse gehören nicht ins Repo, sondern lokal in einen Cache bzw. nach
  Garage `analysis/<slug>/<run_id>/`.
- Analyse-Skripte prüft mypy je Ordner einzeln (Hook `mypy-analysen`).
