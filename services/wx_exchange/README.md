# wx-exchange

Silber-Austausch über die GitHub Container Registry (`docs/ARCHITEKTUR.md` 5.5). Die GFS-Historie
entsteht im GitHub-Actions-Runner statt auf der VM: Ein Lauf überträgt aus dem AWS-Archiv rund
3,3 GB (globale GRIB-Felder, der Ausschnitt ist davon 0,4 %), als Silber bleiben rund 13 MB. Über
die gedrosselte Leitung der VM (8,3 MB/s) kostet ein Lauf mindestens 6–7 Minuten,
im Runner voraussichtlich etwa eine Minute, und auf die VM kommen nur die 13 MB.

```
GitHub Actions (gfs-history.yml)                     VM
  plan ── liest Manifeste in GHCR                     wx-import ── holt fehlende Läufe
  publish JJJJ-MM HH (Matrix, max. 8)                  ↓            nach wx-silver
    AWS-Archiv → Bronze (nur im Runner)               wx-process-backfill ── Gold aus Silber
    → Silber → Tar → GHCR, wenn neu oder geändert
```

## Artefakt

`ghcr.io/the78mole/predictiondb-data/gfs-0p25-silver`, ein Manifest je Monat und Laufzeit, Tag
`JJJJ-MM-HHz` (z. B. `2026-10-00z`), ein Layer je Lauf (Tar des Zarr-Stores `HH.zarr`, Medientyp
`application/vnd.predictiondb.zarr.v3.tar`). Jedes Manifest gehört genau einem Matrix-Job, parallele
Jobs schreiben also nie dasselbe Manifest.

Die Manifeste sind zugleich der Stand. Annotationen je Layer:

| Annotation | Bedeutung |
|---|---|
| `org.predictiondb.run` | Laufzeit (ISO) |
| `org.predictiondb.fingerprint` | Hash der Eingaben: `SILVER_VERSION`, Produkt, Variablen, Ausschnitt, Schritte |
| `org.predictiondb.content` | Inhalts-Hash des Stores ohne den Zeitpunkt des Bronze-Abschlusses |
| `org.predictiondb.silver_version`, `org.predictiondb.steps` | Kodierung, Schrittzahl |

Am Manifest stehen Lizenz (`CC-BY-4.0`), Quellenvermerk und `org.predictiondb.missing`, die Läufe,
die im Archiv fehlen. Sie werden nicht bei jedem Start erneut versucht.

**Nur Lücken:** `plan` nimmt nur Monate auf, in denen Läufe ohne passenden `fingerprint` fehlen,
`publish` lädt in seinem Monat nur diese. Ein abgebrochener Workflow wird mit demselben Aufruf
fortgesetzt; das Manifest wird nach jedem Lauf geschrieben.

**Nur Änderungen hochladen:** Mit `--rebuild` (im Workflow `rebuild`) wird jeder Lauf neu gebaut,
hochgeladen wird er aber nur, wenn sich der Inhalts-Hash geändert hat. Ändern sich die Eingaben
(neue Variable in `config/sources/gfs_0p25.yaml`, neue `SILVER_VERSION`), ändert sich der
`fingerprint`, und der nächste normale Lauf baut die betroffenen Läufe ohnehin neu.

## Workflow

`Actions → GFS-Historie → Run workflow`. Eingaben:

| Eingabe | Standard | |
|---|---|---|
| `start` | `2021-03-22T12` | GFS v16, ältere Läufe unterstützt `wx-ingest` nicht |
| `end` | leer | vor dem 30-h-Fenster von `ingest-gfs` |
| `runs` | `0,12,6,18` | Laufzeiten in Reihenfolge der Priorität |
| `block_years` | `3` | Blöcke vom jüngsten Monat rückwärts |
| `rebuild` | aus | siehe oben |

Reihenfolge: erst Block 1 (die jüngsten drei Jahre), darin alle Monate 00Z, dann 12Z, 06Z, 18Z,
jeweils jüngste zuerst; danach Block 2 mit den älteren Monaten. Höchstens 8 Jobs laufen gleichzeitig,
je Job zwei Läufe parallel mit bis zu 16 Anfragen je Archiv-Endpunkt.

Einmalig nach dem ersten Lauf: Das Paket `predictiondb-data/gfs-0p25-silver` unter
`github.com/the78mole?tab=packages` auf **öffentlich** stellen, damit die VM (und jede andere
Instanz) anonym lesen kann.

## VM

```bash
docker compose --profile history up -d    # wx-import + wx-process-backfill
```

`wx-import` holt stündlich alle Läufe, die lokal nicht als vollständiges Silber der aktuellen
`SILVER_VERSION` vorliegen. Selbst geladenes Silber bleibt unangetastet. `wx-process` erkennt
importierte Läufe ohne Bronze, nimmt das Silber unverändert und rechnet nur Gold (in
`meta.nwp_run` mit `endpoint = ghcr`).

Der Bronze-Backfill (`--profile backfill`) überspringt Läufe, deren Silber schon vorliegt. Wer
trotzdem Bronze auf der VM haben will, setzt `GFS_BACKFILL_BRONZE=true` in der `.env`.

### Bronze behalten und GitHub gegen lokal prüfen

Mit `GFS_BACKFILL_BRONZE=true` und beiden Profilen (`--profile history --profile backfill`) holt
die VM das Silber sofort aus GHCR und lädt Bronze im Hintergrund nach. Sobald für einen Lauf
Bronze vollständig ist, baut `wx-process-backfill` das Silber daraus selbst (das importierte wird
ersetzt). `verify` vergleicht dann den eigenen Bau mit GHCR:

```bash
docker compose run --rm wx-import wx-exchange gfs verify 2021-03-22T12 --runs 0,12
```

Je Lauf: `equal` (gleicher Inhalts-Hash), `different` (für die ersten fünf mit den abweichenden
Dateien bzw. Attributen), `imported` (noch die Kopie aus GHCR, kein eigener Bau) oder
`local_missing`. Verglichen wird ohne Download über die Annotation `content`; nur für Abweichungen
wird der Layer geladen.

## Aufruf

```bash
uv run wx-exchange gfs plan 2021-03-22T12 --runs 0,12,6,18 --block-years 3
WX_EXCHANGE_USERNAME=… WX_EXCHANGE_TOKEN=… uv run wx-exchange gfs publish 2026-09 0
uv run wx-exchange gfs import 2021-03-22T12 --runs 0,12 --watch 3600
```

| Variable | Standard |
|---|---|
| `WX_EXCHANGE_REGISTRY` | `ghcr.io` |
| `WX_EXCHANGE_REPOSITORY` | `the78mole/predictiondb-data/gfs-0p25-silver` |
| `WX_EXCHANGE_USERNAME`, `WX_EXCHANGE_TOKEN` | nur zum Schreiben; Lesen geht anonym |

## Tests

```bash
uv run pytest services/wx_exchange   # offline, mit simulierter Registry und GFS-Spiegel
```

## Offen

- Signatur der Manifeste mit `cosign` (5.5.3).
- `wx-publish` für den laufenden Betrieb der VM (neue Läufe nach GHCR), Bronze-Artefakt.
