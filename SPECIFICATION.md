# PredictionDB — Spezifikation

**Version:** 0.2.0  
**Status:** Entwurf, teilweise umgesetzt  
**Letzte Änderung:** 2026-10-21 (Wärme/Kälte vorgemerkt)

Diese Spezifikation fasst zusammen, **was** PredictionDB leisten soll und was davon heute umgesetzt
ist. Das **Wie** mit Begründungen, Alternativen und Diagrammen steht in
[docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md); Kapitelverweise („A 7.6“) beziehen sich darauf. Bei
Widersprüchen gilt ARCHITEKTUR.md.

Die Anforderungen werden seit 2026-10-30 mit IDs und Rückverfolgbarkeit bis in Code und Tests
unter [reqs/](reqs/README.md) geführt. Die Abschnitte 6 und 7 gehen dort in StRS und SyRS auf und
werden hier nicht mehr fortgeschrieben. Version 0.1 (MariaDB, Gitterwerte als Zeilen, Celery) ist
überholt und steht in der Git-Historie.

---

## 1. Ziel und Umfang

PredictionDB lädt regelmäßig kostenfreie Wettervorhersagen (allen voran NOAA GFS 384 h) per
GRIB2-Partial-Download, speichert sie platzsparend, führt sie mit Energie- und Marktdaten für
Deutschland (Gebotszone DE-LU) und dem Anlagenbestand aus dem Marktstammdatenregister zusammen
und prognostiziert damit:

| Ziel | Auflösung | Horizont | Stand |
|---|---|---|---|
| Netzlast DE | 15 min | 0–16 Tage | geplant (Phase 5–6) |
| Day-Ahead-Preis DE-LU, als Quantile | 15 min | D+1 Kern, bis D+14 Szenario | geplant (Phase 5–6) |
| Wind-, PV- und Laufwasser-Einspeisung DE (Hilfsziele) | 15 min | 0–16 Tage | geplant |
| Erzeugung eigener PV-, Wind- und Wasserkraftanlagen über die API | 15 min / 1 h | bis 384 h, bei Wasser teils länger | geplant (Phase 4b, A 7.6) |
| Wärme- und Kältelast bzw. -bedarf eines Gebäudes und eines Nah-/Fernwärme- oder Kältenetzes | 15 min / 1 h | Folgetag, 24 h, 96 h | vorgemerkt (A 7.8) |
| Strombedarf von Wärmepumpen | 15 min / 1 h | Folgetag, 24 h, 96 h | vorgemerkt (A 7.8) |
| Optimaler Betrieb eines Wärmepumpen-Heizsystems (Preise, Speicher, PV, COP) | 15 min / 1 h | rollierend 24–96 h | vorgemerkt (A 7.8) |

Rahmenbedingungen: Python mit uv, nur kostenfreie Datenquellen (mehrere je Datenart für
Redundanz), Betrieb als Docker-Compose-Stack in einer VM auf Proxmox VE, S3 über Garage
(Single Node), PostgreSQL + TimescaleDB, später migrierbar in die Cloud (A 9). Lizenz: Business
Source License 1.1 mit Additional Use Grant für interne Nutzung ([LICENSE](LICENSE)).

## 2. Begriffe

| Begriff | Bedeutung |
|---|---|
| `run_time` | Startzeit eines Modelllaufs (z. B. GFS 06Z) |
| `valid_time` | Zeitpunkt, für den ein Wert gilt; Vorlauf = `valid_time − run_time` |
| `issue_time` | Zeitpunkt, zu dem eine eigene Prognose erstellt wird |
| `published` | Zeitpunkt, ab dem ein Energiewert öffentlich bekannt war (Leakage-Schutz) |
| Bronze / Silber / Gold | Speicherstufen: Original-GRIB2 / Zarr-Gitter / Features und Energiedaten in der DB |
| Cluster | Gebiet gleichartiger Anlagen je Technologie aus dem MaStR, z. B. `wind_on:v2026Q4:c07` |

Die Ideen der Version 0.1 bleiben erhalten: ein **Register der Merkmale** (früher
`pdid_registry`, jetzt `meta.feature`), ein **Plugin je Quelle** und die Trennung von Gültigkeit und
Vorlauf. Messwerte stehen nicht mehr mit negativem Vorlauf in derselben Tabelle, sondern in
`energy.*` bzw. eigenen Tabellen.

## 3. Datenhaltung

### 3.1 Objektspeicher (Garage, S3-API)

| Bucket | Inhalt | Stand |
|---|---|---|
| `wx-bronze` | GFS-Schritte, auf 44–58° N / 0–20° E zugeschnitten, CCSDS-gepackt: `gfs/0p25/JJJJ/MM/TT/HH/fSSS.grib2` mit Manifest `fSSS.json`, je Lauf `run.json` | umgesetzt |
| `assets-bronze` | MaStR-Gesamtexport unverändert je Stichtag, mit `manifest.json` | umgesetzt |
| `wx-silver` | GFS je Lauf als Zarr v3 (Int16 mit Scale/Offset, Blosc/Zstd, ca. 13 MB je Lauf), Mittel und Summen je Intervall: `gfs/0p25/JJJJ/MM/TT/HH.zarr` | umgesetzt (`wx-process`) |
| `assets-silver` | Gitter-Gewichte je Cluster als Zarr | geplant |
| `ml-artifacts` | MLflow-Artefakte (Modelle) | geplant |
| `ml-exchange` | Trainings-Snapshots und -Ergebnisse für portable Trainingsjobs (A 7.7) | geplant |
| `backups` | pg_dump / WAL-G | geplant |

Zugriff ausschließlich über die S3-API; Endpoint, Region und Schlüssel kommen aus der Umgebung.
`garage-init` legt Layout, Schlüssel und Buckets idempotent an.

### 3.2 Datenbank (PostgreSQL 17 + TimescaleDB)

Schema per Alembic (`migrations/`, Revisionen `0001` → `0002_energy` → `0002_assets` → `0003_wx_features`):

| Schema | Tabellen | Zweck | Stand |
|---|---|---|---|
| `meta` | `source` (mit `priority`, `license`), `nwp_run`, `nwp_feature_run`, `grib_message`, `feature`, `region`, `energy_fetch` | Quellen, Laufzustand, Verarbeitungsstand je Lauf und Clusterung, Merkmalsregister, Regionen, Backfill-Stand | umgesetzt |
| `energy` | `series`, `value` (Hypertable, komprimiert ab 30 Tagen), View `value_best` | Preise, Last, Erzeugung, ÜNB-Prognosen je Quelle; `value_best` wählt je Code und Zeitpunkt die Quelle mit dem kleinsten Rang | umgesetzt |
| `assets` | `snapshot`, `unit`, `gemeinde`, `clustering`, `cluster`, `cluster_cell`, `grid_weight`, `capacity_daily` (Hypertable) | MaStR-Bestand, Verortung, Cluster, Gitter-Gewichte, Tagesbestand | umgesetzt |
| `feat` | `nwp_value` (Hypertable, komprimiert ab 30 Tagen) | Wetter-Features je Merkmal, Cluster-Region, Lauf und Gültigkeit (GFS, `cluster_cap_weighted`) | umgesetzt |
| `fcst` | `prediction` (Hypertable) | eigene Prognosen je Modellversion, Ziel und Quantil | Schema da, Befüllung geplant |
| `assets`, `fcst` (Erweiterung) | `own_plant`, `own_measurement`, `own_curtailment_rule`, `plant_forecast` | eigene Anlagen und ihre Prognosen (A 7.6) | geplant |
| `fcst` (Erweiterung) | `model_spread` | Streuung parallel laufender Modellversionen (A 7.7) | geplant |

Einheiten und Zeit: alle Zeitstempel in UTC, Intervalle am Intervallbeginn, Leistungen in MW (bei
eigenen Anlagen kW), Preise in EUR/MWh.

## 4. Datenquellen

Alle Quellen sind kostenfrei. Jede Quelle ist ein Plugin bzw. eine Quelldefinition mit eigener
Konfiguration in `config/sources/`. Übersicht und Lizenzen in A 3.

| Quelle | Daten | Stand |
|---|---|---|
| NOAA GFS 0,25° (NOMADS-Filter, AWS, Google) | 384-h-Läufe 00/06/12/18Z, 0–120 h stündlich, danach 3-stündlich (209 Schritte) | umgesetzt (`wx-ingest`) |
| ENTSO-E Transparency (Token), SMARD, Energy-Charts | Day-Ahead-Preis, Last, Residuallast, Erzeugung je Träger, ÜNB-Prognosen | umgesetzt (`energy-ingest`), Historie ab 2015 |
| MaStR-Gesamtdatenexport | Wind, PV, Biomasse, Wasser mit Leistung, Datum, Ort, Technik | umgesetzt (`mastr-ingest`), quartalsweise |
| ECMWF IFS/AIFS Open Data, DWD ICON-EU/-D2, GEFS, GEM | weitere Wettermodelle, Ensembles | geplant (Phase 7) |
| Netztransparenz.de (OAuth-Client) | Monats- und Jahresmarktwerte Solar, Wind an Land/auf See, Base | umgesetzt (`energy-ingest`), ab 2012 |
| Netztransparenz.de | Wind-/PV-Prognosen der ÜNB | geplant |
| PEGELONLINE, Pegelportale der Länder, GloFAS | Wasserstand, Abfluss, Abflussprognosen | geplant (A 7.5, 7.6) |
| DWD RADOLAN, DWD CDC | beobachteter Niederschlag, Stationsdaten | geplant |
| Open-Meteo | nur Prototyping und Backfill, nicht produktiv | optional |

GFS-Variablen (umgesetzt, `config/sources/gfs_0p25.yaml`, 22 Felder): `TMP`, `DPT`, `RH` in 2 m,
`UGRD`/`VGRD` in 10 m und 100 m, `GUST`, `DSWRF`, `TCDC`, `APCP`, `CSNOW`, `PRMSL`. Seit 2026-10-21
kommen für Anlagenprognosen (A 7.6) sowie Wärme und Kälte (A 7.8) dazu: `PRES`, `DLWRF`, `SUNSD`,
`WEASD`, `SNOD` am Boden sowie `TSOIL` und `SOILW` in 0–0,1 m und 0,1–0,4 m Tiefe.

## 5. Dienste

Ein Image für alle Python-Dienste (`docker/Dockerfile`), Monorepo als uv-Workspace.

| Dienst (Compose) | Paket / Befehl | Aufgabe | Stand |
|---|---|---|---|
| `postgres` | `timescale/timescaledb:2.31.0-pg17` | Datenbank | umgesetzt |
| `garage` | `dxflrs/garage:v2.4.1` | S3, Single Node | umgesetzt |
| `garage-init` | `pdb-garage-init` | Layout, Schlüssel, Buckets (einmalig) | umgesetzt |
| `migrate` | `alembic upgrade head` | Schema (einmalig) | umgesetzt |
| `ingest-gfs` | `wx-ingest gfs latest --watch 600` | GFS nach Bronze, Endpunkte in Rangfolge NOMADS → AWS → Google | umgesetzt |
| `energy-ingest` | `energy-ingest sync --watch 900` | Abgleich alle 15 min (3 Tage zurück bis 2 Tage voraus), Quellenvergleich, schrittweiser Backfill | umgesetzt |
| `mastr-snapshot` | `mastr-ingest snapshot --if-due --watch 21600` | MaStR je Quartal: Import, Verortung, Cluster, Gewichte, Tagesbestand | umgesetzt |
| `wx-process` | `wx-process gfs latest --watch 600` | Bronze → Silber (Zarr), Wind-, PV- und Wetter-Features je MaStR-Cluster nach `feat.nwp_value` | umgesetzt |
| `ml-predict` | – | Inferenz nach jedem vollständigen Lauf, mehrere Modellversionen parallel | geplant |
| `ml-train` | – | Training bzw. Fine-Tuning als portabler Container-Job (A 7.7) | geplant |
| `api` | FastAPI | Prognosen, Punktabfragen, eigene Anlagen | geplant (Phase 8, Anlagen ab 4b) |
| `mlflow`, `grafana` | – | Modellregistry, Dashboards und Alarme | geplant |

Bibliotheken des Workspace: `packages/core` (Konfiguration `PDB_*`, Logging, S3, DB, Garage-Init),
`packages/gribkit` (`.idx` lesen, Byte-Bereiche zusammenfassen, GRIB2 prüfen und zuschneiden).
Bedienung je Dienst steht in dessen README.

## 6. Funktionale Anforderungen

### 6.1 Wetterdaten

- Neue GFS-Läufe werden spätestens 10 min nach Veröffentlichung eines Schritts erkannt und geladen.
- Nur benötigte Variablen und der Ausschnitt werden übertragen (NOMADS-Filter bzw. Range-Requests).
- Fällt ein Endpunkt aus, wird der nächste versucht. Backfill nutzt nur Archiv-Endpunkte.
- Jeder gespeicherte Schritt hat ein Manifest (Endpunkt, SHA-256, Größe, Nachrichten, fehlende
  Variablen). Ein Lauf gilt erst als vollständig, wenn alle Schritte liegen.
- Bereits gespeicherte Schritte werden nicht erneut geladen, außer Variablenliste oder Ausschnitt
  ändern sich.

### 6.2 Energie- und Anlagendaten

- Mehrere Quellen je Reihe werden parallel gespeichert und gegeneinander geprüft (Warnung ab 5 %
  Abweichung). Für das Lernen gilt die Quelle mit dem kleinsten Rang.
- Jeder Energiewert trägt `published`. Beim Training werden nur Werte verwendet, die zum
  Prognosezeitpunkt bekannt waren.
- Der MaStR-Bestand wird quartalsweise gesichert. Die installierte Leistung je Cluster und Tag wird
  aus Inbetriebnahme, Stilllegung und Wiederaufnahme rekonstruiert, damit das Training den
  historischen Bestand sieht. Cluster werden höchstens jährlich neu gebildet und sind versioniert.

### 6.3 Prognosen

- Zweistufig: zuerst Last, Wind, PV und Laufwasser, daraus die Residuallast, dann der Preis als
  Quantile (A 7.3). Baseline: saisonal-naiv, Analog-Tag, LightGBM.
- Nur Läufe mit `run_time + Verzug ≤ issue_time` gehen ein (A 7.4).
- Wasserkraft berücksichtigt den Nachlauf von Niederschlag, Schnee und Boden (A 7.6).
- Mehrere Modellversionen laufen parallel. Ihre Streuung wird gespeichert, als Unsicherheitsmaß
  ausgegeben und bei Ausreißern gemeldet (A 7.7).

### 6.4 Eigene Anlagen (API, A 7.6)

| Methode und Pfad | Zweck |
|---|---|
| `POST /plants` | Anlage anlegen: `pv` (Teilflächen mit kWp, Azimut, Neigung), `wind` (Nabenhöhe, Rotordurchmesser, Nennleistung, Leistungskurve) oder `hydro` (Fallhöhe, Ausbaudurchfluss, Turbinentyp, Restwasser, Pegel bzw. Einzugsgebiet) |
| `GET/PATCH/DELETE /plants/{id}` | Stammdaten lesen, ändern, Anlage samt Daten löschen |
| `PUT /plants/{id}/measurements` | Messwerte hochladen (CSV/JSON, 15 min oder 1 h, typisch ein Jahr) |
| `PUT /plants/{id}/curtailment-rules` | Abschaltregeln (Fledermäuse, Vögel, Schall, Restwasser, Abregelung) |
| `POST /plants/{id}/calibrate`, `GET /plants/{id}/calibration` | Kalibrierung gegen Hindcasts anstoßen und abfragen |
| `GET /plants/{id}/forecast` | Prognose mit Quantilen, mit und ohne Abschaltung |
| `GET /plants/{id}/backtest` | Güte gegen die eigenen Messwerte |

Ohne Messwerte liefert ein physikalisches Modell (pvlib, windpowerlib, Abfluss × Fallhöhe) eine
Prognose. Messwerte eigener Anlagen werden nur lokal verarbeitet.

### 6.5 Training (A 7.7)

- Regelfall ist das wöchentliche Nachtrainieren des Produktionsmodells (Fine-Tuning mit
  Replay-Stichprobe). Neu trainiert wird bei Strukturänderungen (neue Clusterversion, neue
  Features oder Quellen, andere Zielauflösung) oder dauerhaft steigendem Fehler.
- Ein neues Modell ersetzt das bisherige erst nach einem Champion/Challenger-Vergleich.
- Training läuft als portabler Container-Job auf CPU, lokaler GPU oder gemieteter GPU. Ein Job
  bekommt nur einen Trainings-Snapshot über zeitlich begrenzte Links und braucht keinen Zugang ins
  Heimnetz.

### 6.6 Wärme, Kälte und Wärmepumpen (vorgemerkt, A 7.8)

Geplant, Architektur folgt später:

- Wärme- und Kältelast bzw. -bedarf für ein Gebäude und für ein Nah-/Fernwärme- oder Kältenetz,
  jeweils für den Folgetag sowie die kommenden 24 h und 96 h.
- Strombedarfsprognose von Wärmepumpen.
- Ermittlung des optimalen Betriebs eines Wärmepumpen-Heizsystems aus Strompreisen, Wärmespeicher
  und Speicherfüllstand, PV-Prognose und der wetterabhängigen Effizienz (COP).

Die dafür nötigen Wetterdaten (Temperatur, Feuchte, Wind, kurz- und langwellige Strahlung,
Sonnenscheindauer, Bodentemperatur, Schnee) werden bereits gesammelt (Abschnitt 4).

## 7. Nichtfunktionale Anforderungen

| Bereich | Anforderung |
|---|---|
| Speicher | Wetter als GRIB2/Zarr im Objektspeicher, nicht zeilenweise in der DB (Größenordnung 15 GB je Jahr statt Milliarden Zeilen, A 5.4) |
| Betrieb | VM mit 6–8 vCPU, 24–32 GB RAM, 500 GB Daten (A 8.1); alle Dienste in Compose, Konfiguration über `.env` (Präfix `PDB_`) |
| Robustheit | Jeder Dienst ist wiederanlauffähig und idempotent; Zustand liegt in der DB bzw. in Manifesten |
| Beobachtbarkeit | strukturierte JSON-Logs (`structlog`), später Prometheus-Metriken und Grafana-Alarme |
| Inferenz | Sekunden je Lauf; Training im Betrieb unkritisch, in der Entwicklung möglichst unter 1 h |
| Portabilität | nur S3-API, Postgres-Protokoll und 12-Factor-Konfiguration, damit der Umzug in die Cloud reine Konfiguration ist |
| Lizenzen | Quellenvermerke in `meta.source.license`, in API-Antworten und Dashboards |
| Datenschutz | Standort und Messwerte eigener Anlagen sind personenbezogen, werden nicht mit dem MaStR verknüpft und lassen sich vollständig löschen |

## 8. Konfiguration

Wichtige Variablen (vollständig in `.env.example` und den READMEs der Dienste):

| Variable | Zweck |
|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `PDB_DATABASE_URL` | Datenbank |
| `GARAGE_RPC_SECRET`, `GARAGE_ADMIN_TOKEN`, `PDB_GARAGE_CAPACITY` | Garage |
| `PDB_S3_ENDPOINT_URL`, `PDB_S3_REGION`, `PDB_S3_ACCESS_KEY_ID`, `PDB_S3_SECRET_ACCESS_KEY` | S3-Zugang der Dienste |
| `PDB_ENTSOE_TOKEN`, `PDB_ENERGY_SOURCES`, `PDB_ENERGY_BACKFILL_FROM` | Energiedaten |
| `PDB_MASTR_CONFIG`, `PDB_MASTR_WORK_DIR` | MaStR-Import |
| `PDB_WX_PROCESS_CONFIG` | Wetter-Verarbeitung |
| `WX_GFS_CONFIG` | GFS-Quelldefinition |
| `PDB_LOG_LEVEL`, `PDB_LOG_JSON` | Logging |

## 9. Umsetzungsstand

| Phase (A 11) | Inhalt | Stand |
|---|---|---|
| 0 – Spike | Bibliotheken, Mengengerüst | entschieden: eigenes `gribkit` statt Herbie; Mengengerüst noch an echten Läufen messen |
| 1 – Fundament | uv-Workspace, Compose mit TimescaleDB und Garage, Alembic, `core`, CI (Lint, Typen, Tests, Image) | erledigt |
| 2 – GFS-Ingest | `wx-ingest` mit NOMADS, AWS, Google; Backfill | erledigt, Backfill der Historie offen |
| 3 – Energiedaten | `energy-ingest`, `mastr-ingest` | erledigt |
| 4 – Processing | `wx-process`: Silber-Zarr, Cluster-Features | erledigt für GFS; bevölkerungsgewichtete Temperatur offen |
| 4b – Anlagenprognose | API für eigene PV-, Wind- und Wasserkraftanlagen | offen |
| 5 – Baseline-ML | LightGBM, Evaluierung, MLflow | offen |
| 6 – NN | Sequenzmodelle, Quantile, Inferenzdienst, Grafana | offen |
| 7 – Weitere Quellen | ECMWF, ICON, GEFS, GEM, Pegel | offen |
| 8 – API & Härtung | FastAPI, Auth, Alarme, Backups, Cloud-Probelauf | offen |
| später – Wärme/Kälte | Gebäude, Netze, Wärmepumpen, Betriebsoptimierung (A 7.8) | vorgemerkt; Wetterdaten werden gesammelt |

Offene Entscheidungen stehen in A 12.
