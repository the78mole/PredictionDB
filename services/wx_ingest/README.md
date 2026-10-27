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
   `header_scan` (Archiv ohne `.idx`, NCAR GDEX d084001 in `config/sources/gfs_0p25_gdex.yaml`):
   je Nachricht die ersten 512 Byte lesen (Länge aus Sektion 0, Parameter, Fläche und
   Prognosezeit aus Sektion 4, wgrib2-Namen wie in der `.idx`), danach weiter wie `idx_range`.
   Damit die Köpfe nicht nacheinander gelesen werden, wird die Datei in bis zu 16 Abschnitte
   geteilt; an jedem Schnitt wird der nächste Nachrichtenanfang (`7777GRIB`) gesucht. Landet der
   Lauf über die Köpfe nicht genau am nächsten Anfang, wird die ganze Datei geladen.
   Das vollständige Inventar eines Schritts dient als Vorlage für denselben Schritt späterer
   Läufe (Reihenfolge innerhalb einer Modellversion fest): Die Lage der gewählten Nachrichten
   wird aus der Vorlage geschätzt, dort angesprungen und nur über wenige Köpfe gelaufen, statt
   alle Köpfe zu lesen. Passt die Vorlage nicht, entsteht ein neues vollständiges Inventar.
   In GitHub Actions baut `wx-exchange gfs templates` die Vorlagen einmal je Workflow-Lauf
   (Job „Kopf-Inventare“, Artefakt `gfs-templates`); die Monatsjobs übernehmen sie mit
   `publish --templates`.
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
uv run wx-ingest gfs backfill 2021-03-22T12 --runs 0,12 --newest-first --parallel-runs 2
```

## Historie (Backfill)

`backfill START [ENDE]` lädt alle konfigurierten Läufe im Zeitraum nur von Archiv-Endpunkten.
Ohne `ENDE` gilt „jetzt“. `--runs` wählt Laufzeiten (z. B. `0,12`), `--newest-first` beginnt mit
den jüngsten Läufen, `--parallel-runs N` lädt mehrere Läufe gleichzeitig (die Grenzen je Endpunkt
gelten für alle zusammen). Vollständige Läufe kosten nur einen Blick auf `run.json`; ein
abgebrochener Backfill wird mit demselben Aufruf fortgesetzt. Läufe, die im Archiv fehlen,
bleiben `pending` und werden nicht als Fehler gewertet.

Jeder Aufruf protokolliert sich in `meta.backfill_job` (Zeitraum, Läufe, Fortschritt, geladene
Bytes, Status). Das Status-Dashboard zeigt daraus die laufenden und die letzten zehn beendeten
Jobs. Ohne erreichbare Datenbank wird nur gewarnt und weitergeladen; `--no-job-log` schaltet das
Protokoll ab. Ein Job, der ohne Abschluss endet (Container hart beendet), gilt beim nächsten Start
nach einer Stunde ohne Lebenszeichen als abgebrochen.

Läufe, deren Silber schon vorliegt (z. B. aus GHCR importiert, `services/wx_exchange`), lädt der
Backfill nicht; `--bronze-with-silver` bzw. `GFS_BACKFILL_BRONZE=true` lädt auch für sie Bronze.

Für den Dauerbetrieb gibt es das Compose-Profil `backfill` (`gfs-backfill` und
`wx-process-backfill`, Zeitraum und Läufe über `GFS_BACKFILL_START`, `GFS_BACKFILL_RUNS`,
`GFS_BACKFILL_PARALLEL`).

Stand AWS-Archiv `noaa-gfs-bdp-pds` (geprüft 2026-10-21):

| Zeitraum | Modell | Pfad | Vollständigkeit |
|---|---|---|---|
| ab 2021-03-22 12Z | GFS v16 | `gfs.JJJJMMTT/HH/atmos/` | alle 8154 Läufe bis 2026-10-20 vollständig mit `.idx` |
| 2021-02-26 – 2021-03-22 06Z | GFS v15 | `gfs.JJJJMMTT/HH/` | vollständig, f000 ohne `TCDC`; wird nicht unterstützt |
| 2021-01-01 – 2021-02-25 | GFS v15 | `gfs.JJJJMMTT/HH/` | häufig nur 3-stündlich |

Je Lauf werden mit 22 Feldern rund 3,3 GB in 3341 Range-Anfragen übertragen. Ab Bronze bleiben
davon rund 22 MB, und das Zuschneiden kostet rund 0,6 CPU-Sekunden je Schritt (gemessen an 0Z-Läufen
im Oktober 2026).

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
