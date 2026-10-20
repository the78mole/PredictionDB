# wx-ingest

Lädt NOAA GFS 0,25° (384-h-Läufe) per GRIB2-Partial-Download und legt die auf den
Ausschnitt zugeschnittenen Schritte in Garage (Bucket `wx-bronze`) ab. Grundlage:
`docs/ARCHITEKTUR.md` Kap. 4, 5.1 und 6.3–6.4; Konfiguration in `config/sources/gfs_0p25.yaml`.

## Ablauf je Schritt

1. Endpunkte in Konfigurationsreihenfolge versuchen (NOMADS-Filter → AWS → Google).
2. `nomads_filter`: Variablen, Level und Ausschnitt serverseitig filtern.
   `idx_range`: `.idx` laden, Nachrichten wählen, je Byte-Bereich eine Range-Anfrage
   (Multi-Range liefert bei S3 die ganze Datei), Antwortlänge und GRIB-Struktur prüfen,
   lokal zuschneiden.
   NOMADS liefert Süd→Nord und einfache Packung; die Antwort wird auf Nord→Süd gedreht
   und verlustfrei mit CCSDS gepackt, damit Bronze unabhängig vom Endpunkt einheitlich ist.
   Der Filter kombiniert jede Variable mit jedem Level und liefert daher einzelne
   zusätzliche Felder (derzeit `TMP` in 100 m und am Boden).
3. `gfs/0p25/JJJJ/MM/TT/HH/fSSS.grib2` schreiben, danach das Manifest `fSSS.json`
   (Endpunkt, URL, SHA-256, Größe, Nachrichten, im Schritt fehlende Variablen).
4. Liegen alle 209 Schritte, entsteht `run.json` als Laufmanifest.

Noch nicht veröffentlichte Schritte bleiben `pending`; im Live-Modus wird nach dem ersten
fehlenden Schritt nicht weiter gefragt. Schritte mit passendem Manifest werden übersprungen,
ändert sich die Variablenliste oder der Ausschnitt, werden sie neu geladen.

## Aufruf

```bash
# Zugang wie alle Dienste über predictiondb_core (.env bzw. Umgebung, Präfix PDB_)
export PDB_S3_ENDPOINT_URL=http://localhost:3900 PDB_S3_ACCESS_KEY_ID=… PDB_S3_SECRET_ACCESS_KEY=…
uv run wx-ingest gfs latest                  # unvollständige Läufe der letzten 30 h
uv run wx-ingest gfs latest --watch 600      # dauerhaft alle 10 min
uv run wx-ingest gfs run 2026102006 --steps 0-6
uv run wx-ingest gfs backfill 2026-10-01T00 2026-10-07T18   # nur Archiv-Endpunkte (AWS, Google)
```

| Variable | Standard |
|---|---|
| `PDB_S3_ENDPOINT_URL` | `http://localhost:3900` |
| `PDB_S3_REGION` | `garage` |
| `PDB_S3_ACCESS_KEY_ID`, `PDB_S3_SECRET_ACCESS_KEY` | – |
| `PDB_BUCKET_BRONZE` | `wx-bronze` |
| `PDB_LOG_LEVEL`, `PDB_LOG_JSON` | `INFO`, `true` |
| `WX_GFS_CONFIG` | `config/sources/gfs_0p25.yaml` |

## Tests

```bash
uv run pytest services/wx_ingest                 # offline, mit simulierten Spiegeln
uv run pytest services/wx_ingest -m network      # je ein echter Schritt von AWS und NOMADS
```
