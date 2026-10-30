# wx-exchange

Silber-Austausch über die GitHub Container Registry (`docs/ARCHITEKTUR.md` 5.5). Die GFS-Historie
entsteht im GitHub-Actions-Runner statt auf der VM: Ein Lauf überträgt aus dem AWS-Archiv rund
3,3 GB (globale GRIB-Felder, der Ausschnitt ist davon 0,4 %), als Silber bleiben rund 13 MB. Über
eine Leitung mit 8 MB/s kostet ein Lauf mindestens 6–7 Minuten, im Runner etwa eine Minute, und
auf die VM kommen nur die 13 MB.

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
| `source` | `aws` | `aws`: GFS v16 aus `noaa-gfs-bdp-pds`; `gdex`: ältere Läufe aus NCAR GDEX d084001 (`config/sources/gfs_0p25_gdex.yaml`) |
| `start` | `2021-03-22T12` bzw. `2019-06-12T12` | bei `gdex` ab GFS v15 (Pilot); das Archiv reicht bis 2015-01-15 zurück |
| `end` | leer bzw. `2021-03-22T06` | bei `aws` vor dem 30-h-Fenster von `ingest-gfs` |
| `runs` | `0,12,6,18` | Laufzeiten in Reihenfolge der Priorität |
| `block_years` | `3` | Blöcke vom jüngsten Monat rückwärts |
| `rebuild` | aus | siehe oben |

**GDEX (vor GFS v16):** dreistündlich bis 240 h, danach zwölfstündlich (93 statt 209 Schritte), ohne
`.idx`; das Inventar entsteht aus den Nachrichtenköpfen (`header_scan`, siehe `services/wx_ingest`).
Je Lauf werden so rund 1,5 GB statt 20–30 GB übertragen (gemessen: 4–8 s je Schritt einzeln, 8 Schritte parallel in 25 s). Silber trägt dieselben Variablen; in `ml_train`
markiert die Merkmalsgruppe `nwp_version` (`wx_gfs_v`) die GFS-Version, weil der 10-m-Wind von v15 zu
v16 einen Bias-Sprung hat (`docs/analysen/gfs-v15-v16`). Im März 2021 teilen sich beide Quellen die
Tags; `start`/`end` trennen sie, der Workflow läuft nie zweimal gleichzeitig.

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
`SILVER_VERSION` vorliegen. Selbst geladenes Silber bleibt unangetastet. Den lokalen Bestand meldet
er nach `meta.silver_run` (zu Beginn eines Durchgangs alle zu holenden Läufe als `loading`, jeden
geholten als `present`); `wx-process` meldet dort jedes Silber, das es baut oder liest. Daraus
zeigt das Dashboard „Wetter“ je Monat den Anteil der GFS-Läufe mit lokalem Silber (grün) und im
Download (gelb, dazu laufende Bronze-Downloads aus `meta.backfill_run`). `wx-process` erkennt
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

## Vollständige .idx zu GDEX

Das NCAR-Archiv GDEX d084001 (GFS vor v16) hat keine `.idx`. Der Workflow `gfs-idx.yml` liest
je Datei alle Nachrichtenköpfe und schreibt eine `.idx` im Format von `wgrib2 -s` mit allen
Nachrichten (geprüft: an GFS-v15-Dateien auf AWS Zeile für Zeile gleich den `.idx` von NOAA).
Abgelegt wird ein einziges Artefakt mit einem Layer je Monat und Laufzeit:

```text
ghcr.io/the78mole/predictiondb-data/gfs-0p25-gdex-idx:v1
  Layer gfs-0p25-gdex-idx-2019-07-00z.tar.gz   org.predictiondb.month=2019-07, .hour=0
        └─ 2019/20190701/gfs.0p25.2019070100.f000.grib2.idx …
```

Die Annotationen eines Layers tragen den Stand: `complete`, `failed` (Dateien, die der nächste
Start nachholt), `absent` (fehlen im Archiv), `broken` (im Archiv leer oder abgeschnitten, werden
nicht wiederholt) und `unknown` (Zeilen mit Namen, die nicht in der
NCEP-Tabelle stehen). Der Abschlussjob schreibt das Manifest auch, wenn einzelne Monatsjobs
scheitern; ein erneuter Start nimmt nur Monate ohne vollständigen Layer und übernimmt dabei schon
gebaute `.idx`. Ein Prozess schafft nur einen Kern; auf einer ausgelasteten Maschine teilt
`idx-build --workers N` Dateien und gleichzeitige Anfragen auf N Prozesse auf (gleiche Last auf
das Archiv). Einen Monat holen:

```bash
oras manifest fetch ghcr.io/the78mole/predictiondb-data/gfs-0p25-gdex-idx:v1 \
  | jq -r '.layers[] | select(.annotations["org.predictiondb.month"]=="2019-07") | .digest'
oras blob fetch -o 2019-07-00z.tar.gz ghcr.io/the78mole/predictiondb-data/gfs-0p25-gdex-idx@sha256:…
```

`publish` nutzt den Layer selbst: Vor einem Monat lädt es den passenden Layer (Monat und
Laufzeit) und liest die gewählten Nachrichten wie bei `idx_range` direkt über die `.idx`, ohne
Köpfe abzulaufen (Log `idx_loaded`). Dateien ohne `.idx` (noch `failed`) und `.idx`, die nicht
zur Datei passen, laufen weiter über Vorlagen und Köpfe. `--idx-repository ''` schaltet das ab.

## GEFS-Ensemble

`wx-exchange gefs` baut das Silber des GEFS v12 (0,5°, 31 Mitglieder, 00Z, bis 384 h, Verbundnetz Europa) direkt
aus dem AWS-Archiv, ohne Bronze: je Mitglied-Schritt nur die Felder aus
`config/sources/gefs_0p5.yaml` per Byte-Bereich, verdichtet je Region aus
`config/gefs/regions_v2.json.gz`. Nach GHCR geht je Monat ein Manifest mit einem Layer je Lauf
(`ghcr.io/the78mole/predictiondb-data/gefs-0p5-silver:JJJJ-MM-00z`, Inhalt siehe
`docs/ARCHITEKTUR.md` 5.6). Der Workflow `gefs-history.yml` füllt die Historie ab 2021-03-22
mit abgeschlossenen Monaten; den aktuellen Monat baut die VM selbst.

Auf der VM läuft `wx-exchange gefs vm` als Dienst `wx-gefs` (alle 30 min):

1. **Live:** Läufe des aktuellen Monats (und der letzten 3 Tage des Vormonats), sobald der
   letzte Schritt des letzten Mitglieds auf AWS liegt. Dabei landen die Nachrichten auf den
   Ausschnitt zugeschnitten in `wx-bronze` (`gefs/0p5/JJJJ/MM/TT/HH/<mitglied>.fSSS.grib2`), das
   Silber in `wx-silver` (`gefs/0p5/JJJJ/MM/TT/HH.tar.gz`, byte-gleich mit dem Layer) und in GHCR.
2. **Import:** abgeschlossene Monate aus GHCR, nur Layer mit aktuellem Fingerabdruck.
3. **Gold:** je Lauf Mittel, Streuung, P10 und P90 über die Mitglieder je Region, Größe und
   Schritt nach `feat.nwp_value` (Merkmal `<größe>_<kennzahl>`, Aggregation `ensemble`, Region
   `gefs:<code>`), jüngste Läufe zuerst.
4. **Bronze aufräumen:** nach `--bronze-days` (183), aber nur, wenn das Silber des Laufs lokal
   und in GHCR mit gleichem Inhalts-Hash liegt; sonst bleibt sie und das Log meldet
   `gefs_bronze_kept`.

```bash
uv run wx-exchange gefs plan 2021-03-22T12
WX_EXCHANGE_USERNAME=… WX_EXCHANGE_TOKEN=… uv run wx-exchange gefs publish 2026-09 0
# neue Regionen-Datei (MaStR-CSV aus config/gefs/mastr_cells.sql)
uv run wx-exchange gefs regions --mastr de_capacity_0p5.csv --natural-earth ne/ --version v2 \
  --out config/gefs/regions_v2.json.gz
```

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
| `WX_EXCHANGE_GEFS_REPOSITORY` | `the78mole/predictiondb-data/gefs-0p5-silver` (nur `gefs`) |
| `WX_EXCHANGE_USERNAME`, `WX_EXCHANGE_TOKEN` | nur zum Schreiben; Lesen geht anonym |

## Tests

```bash
uv run pytest services/wx_exchange   # offline, mit simulierter Registry und GFS-Spiegel
```

## Offen

- Signatur der Manifeste mit `cosign` (5.5.3).
- `wx-publish` für den laufenden Betrieb der VM (neue Läufe nach GHCR), Bronze-Artefakt.

## Öffentlicher Grafana-Snapshot (`wx-exchange snapshot`)

Dienst `grafana-snapshot` (Compose-Profil `public`): täglich um `PDB_SNAPSHOT_AT` (Standard
14:30 Ortszeit, nach Day-Ahead-Auktion und Prognoselauf) holt er das Dashboard „Prognose
aktuell“ im Schema v2 aus der eigenen Grafana, führt die Abfragen der Wochenpanels (67–70) für
heute 00:00 bis Ende D+7 aus, bettet die Frames als Abfrage `queryType: snapshot` ein und lädt
das Ganze über `POST /api/snapshots` mit `external: true` auf snapshots.raintank.io hoch
(Ablauf `PDB_SNAPSHOT_EXPIRES`, Standard 2 Tage). Der Snapshot bleibt interaktiv (Tooltips, Zoom,
Legende), hat aber jedes Mal eine neue Adresse. Deshalb biegt der Dienst danach einen Kurzlink um:
`PDB_SHORTLINK_KIND=shortio` (Short.io, `PDB_SHORTIO_LINK_ID`) oder `shlink` (`PDB_SHLINK_URL`,
`PDB_SHLINK_CODE`), Schlüssel in `PDB_SHORTLINK_API_KEY`. Ohne Kurzlink wird nur hochgeladen.

Gestern/Heute/Morgen bleiben draußen: Ein Snapshot hat einen festen Zeitraum, und dann ignoriert
Grafana relative Panel-Zeiträume. Einmalig von Hand: `docker compose --profile public run --rm
grafana-snapshot wx-exchange snapshot --once`.
