# gribkit

Werkzeuge für GRIB2-Partial-Downloads (siehe `docs/ARCHITEKTUR.md`, Kap. 4 und 6.1):

- `parse_idx`, `select`, `merge_ranges`: NCEP-`.idx`-Inventar lesen, Nachrichten über
  `VAR:LEVEL` (optional Regex auf das Prognosefeld) wählen und zu Byte-Bereichen zusammenfassen.
- `iter_messages`: GRIB2-Bytestrom in Nachrichten zerlegen und auf Vollständigkeit prüfen.
- `crop`: `regular_ll`-Nachrichten mit ecCodes auf einen `BBox`-Ausschnitt zuschneiden
  (auch über den Nullmeridian); Packungsart und Genauigkeit der Quelle bleiben erhalten.

Hinweis: Die S3-Endpunkte von AWS und Google beantworten Multi-Range-Anfragen
(`Range: bytes=a-b,c-d`) mit der ganzen Datei. Deshalb lädt der Aufrufer jeden
Bereich aus `merge_ranges` als eigene Anfrage.

```bash
uv run --package gribkit pytest packages/gribkit
```
