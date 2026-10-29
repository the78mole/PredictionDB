# Strompreisprognose – Architekturentwurf

**Version:** 0.12 (Entwurf) · **Stand:** 2026-10-28 · **Status:** zur Diskussion

---

## 0. Kurzfassung

- **Ziel:** Ein Docker-Compose-Stack auf einer PVE-VM, der Wettervorhersagen (allen voran NOAA GFS 384 h) per GRIB-Partial-Download lädt, platzsparend ablegt, mit Last-, Erzeugungs- und Preisdaten für Deutschland (Gebotszone DE-LU) zusammenführt und daraus ein neuronales Netz für **Strombedarf** und **Day-Ahead-Preis** trainiert und betreibt.
- **Speicher in drei Stufen (Medaillon):**
  1. **Bronze:** zugeschnittene Original-GRIB2-Ausschnitte (Europa/DE-Box, nur benötigte Variablen) in **Garage (S3)**. Quelle der Wahrheit, jederzeit neu auswertbar.
  2. **Silber:** einheitliches Gitter je Lauf als **Zarr** (float16/Scale-Offset + Zstd) in Garage. Grundlage für Training und Regionen-Aggregation.
  3. **Gold:** aggregierte Feature-Zeitreihen (DE-weit und je Regelzone/Bundesland, kapazitäts- bzw. bevölkerungsgewichtet) plus Energie-Marktdaten in **PostgreSQL + TimescaleDB** (komprimierte Hypertables).
- **Modell-Releases:** Trainierte Modelle werden mit Modellkarte als eigene Artefakte in GHCR veröffentlicht. Sie lernen dafür nur aus Quellen, deren Bedingungen das zulassen (NOAA, DWD, ECMWF, MaStR, SMARD, Energy-Charts, ENTSO-E-Lastprognose); unfreie Quellen wie Netztransparenz bleiben außen vor (7.11).
- **Datenaustausch:** Verdichtete Wetterdaten (Silber, optional Bronze) werden als OCI-Artefakte in GHCR veröffentlicht; weitere Instanzen importieren sie, statt GFS selbst zu laden. Ohne Bronze verlieren sie für Features, Training und Anlagenprognose nichts, nur den eigenen Neuaufbau von Silber (5.5).
- **Gitterdaten gehören nicht zeilenweise in die Datenbank.** Eine Zeile pro Gitterpunkt × Variable × Zeitschritt × Lauf (wie im bestehenden PredictionDB-Entwurf) würde allein für GFS über Deutschland mehrere Milliarden Zeilen pro Jahr erzeugen (Rechnung in 5.4).
- **Prognose eigener Anlagen:** Über die API lassen sich einzelne PV-, Windkraft- und Wasserkraftanlagen mit Stammdaten und optional einem Jahr Messwerten anlegen; sie bekommen eine Erzeugungsprognose aus denselben Wetterdaten, physikalisch gerechnet und mit den Messwerten kalibriert (7.6).
- **Repository:** `the78mole/PredictionDB` (Nachfolger der dortigen `SPECIFICATION.md` v0.1).
- **Python mit uv**, ein uv-Workspace mit mehreren Paketen; jeder Dienst ein eigenes, schlankes Image.
- **Steuerung über ENV, Konfigurationsdateien und CLI.** Eine Steuer-UI (SPA mit FastAPI-Backend) ist optional zuschaltbar; ENV-Variablen haben immer Vorrang, das System läuft vollständig ohne sie ([STEUER-UI.md](STEUER-UI.md)).
- **Cloud-Migration** ist von Anfang an mitgedacht: nur S3-API, Postgres-Protokoll und 12-Factor-Konfiguration. Garage → AWS S3/R2/Hetzner, Postgres → Managed Postgres (Timescale Cloud o. Ä.), Compose → Kubernetes/Nomad.

---

## 1. Ausgangslage

### 1.1 Vorgaben (aus dem Projekt-Thread)

| Vorgabe | Umsetzung im Entwurf |
|---|---|
| GRIB-Downloader neu in Python mit uv | uv-Workspace, Paket `wx-ingest` (Kap. 6) |
| Möglichst alle Quellen, nur kostenfreie, auch zur Redundanz | Quellenmatrix mit Primär-/Ausweich-Endpunkten je Modell (Kap. 3) |
| Zunächst separate VM auf PVE, alles in Docker, provisioniert per Docker Compose | Compose-Stack (Kap. 8) |
| Spätere Cloud-Migration | Kap. 9 |
| S3 über Garage, einzelner Knoten genügt | Garage Single-Node, `replication_factor = 1` (Kap. 5.2) |

### 1.2 Vorhandene Grundlagen in den Repos

Als Vorlage dient ein Altsystem aus den Jahren 2012/2013 (Java/OSGi-Clients und serverseitige Shell/CGI-Skripte). Es wird nur konzeptionell ausgewertet, Code wird nicht übernommen.

| Teil des Altsystems | Inhalt | Bedeutung für dieses Projekt |
|---|---|---|
| GRIB-Bundle (Java) | UCAR GRIB-Java 8.0.29, öffnet Dateien nur, kein Parser, kein Downloader. Vier Testdateien: GFS 1,0° 2-m-Temperatur 2012-12-20 00Z (3–192 h), GDAS-Analyse, Nordeuropa-Wind (GRIB1). | Kein Code; Testdateien eignen sich als Fixtures |
| Wetter-Client (Java) | HTTP-Client für den damaligen GFS-Archivserver | Zeigt die damalige Schnittstelle (siehe unten) |
| Assessments (Java) | Bewerten, wie ähnlich ein historischer Tag dem Prognosetag ist (2-m-Temperatur, Globalstrahlung DSWRF), damit die Verbrauchsprognose ähnliche Tage stärker gewichtet | Analog-Tag-Idee als Baseline/Feature (7.3) |
| Server-Dokumentation | Beschreibung der Download- und Abfrageskripte (2012/2013); die Skripte selbst sind nicht erhalten | Ablauf und Lehren, siehe unten |

**Wichtigster Befund:** Einen Java-GRIB-Downloader gab es nicht. Herunterladen und Zerlegen liefen serverseitig in Shell/CGI-Skripten. Die Java-Bundles waren nur Clients dieses Servers:

- `grib_finished.txt`: Liste vollständig geladener Läufe (`yyyyMMddHH`, eine Zeile je Lauf).
- `cgi-bin/get_cgi_data.sh?<inventarID|Kurzname>&<Ort|lon,lat>&<Lauf>`: Zeitreihe eines Parameters an einem Ort, Textformat `# Date: yyyyMMddHH`, dann Zeilen `<Vorlauf h>:<…>:<Wert>`; `?idlist`, `?loclist`, `?help`.
- Datenbasis: GFS global 1,0°, 3-h-Schritte bis 192 h, eine Datei je Lauf und Parameter (`2012122000_195.grb`).
- Ablauf: Cron `20 0,6,12,18 * * *` startete das Download-Skript. Die Daten kamen von NOAA `…/gfs/prod/`, ab August 2013 vom NOMADS-Mirror. Geladen wurde per curl mit **einem Range-Request je GRIB-Nachricht**, das waren ca. 4 200 Requests und etwa 1,5 h je Lauf. Die Werte für einen Punkt holte ein C-Programm (grib_api) heraus.
- Die Konfiguration führte die abzurufenden Parameter als Inventar-Nummern und prüfte, ob NOAA das Produktlayout geändert hatte. Das bestätigt, dass Inventar-Nummern brüchig sind. Neu wird deshalb über Kurzname und Level adressiert (6.5).
- Für Stationsdaten gab es zusätzlich einen DWD-Abruf. Dem entspricht im neuen Entwurf die DWD-CDC-Quelle als Wetter-„Wahrheit“ (3.1).

Lehren für den Neubau: benachbarte Byte-Bereiche zu wenigen Requests zusammenfassen bzw. den NOMADS-Grib-Filter nutzen (4.1). So schrumpfen die ~4 200 Requests auf einige Hundert je Lauf. Außerdem den Platz vor dem Download prüfen und Läufe erst nach Vollständigkeit freigeben (damals `grib_finished.txt`, neu `meta.nwp_run`).

**Spätere Ausbaustufe (2014):** Statusmails aus dem Jahr 2014 zeigen, dass das Altsystem die GFS-Punktwerte später zusätzlich in eine MySQL-Tabelle schrieb (Spalten etwa `id, meta_id, time, Wert`, Metadaten über `meta_id`), statt sie nur bei Abfrage aus den GRIB-Dateien zu lesen. Das ist dieselbe Idee wie `datapoints_*` in der `SPECIFICATION.md` und wie die Gold-Schicht hier. Der Neubau geht aber deutlich weiter: Gitterdaten bleiben als GRIB2/Zarr in Garage, und nur abgeleitete Punkt- und Regionsreihen landen in TimescaleDB-Hypertables mit Kompression (5.3, 5.4). Der Ansatz von 2014 dient nur als Bestätigung des Musters, nicht als Vorlage für das Schema.

Ebenfalls ausgewertet:

| Repo | Inhalt | Bedeutung für dieses Projekt |
|---|---|---|
| `the78mole/PredictionDB` (damals privat) | Nur `SPECIFICATION.md` v0.1 (Juli 2026): MariaDB + Python-Worker mit Plugin-Konzept je Quelle, Tabellen `pdid_registry` und `datapoints_{int,float,varchar}`, Schlüssel `(pdid, lat, lon, timestamp, offset)`; als Schnittstelle für ein Prognosesystem gedacht. | Direkter Vorläufer. Plugin-Idee, PDID-Register und die Semantik „Gültigkeitszeit + Vorlauf, negativer Offset = Messung“ übernehme ich. Das zeilenweise Speichern von Gitterwerten und MariaDB ersetze ich (Begründung in 5.4). |
| `the78mole/test-meteo` | Streamlit-Demo, Open-Meteo-Abfrage für Wind in 120 m bei Cuxhaven | Bestätigt Open-Meteo als Schnellstart-Quelle |
| `the78mole/eprice-demo` | Rust-Demo, Day-Ahead-Preise DE über `api.energy-charts.info` | Bestätigt Energy-Charts als Preisquelle |

Hinweis zu PredictionDB: Die dort genannte NOMADS-URL `filter_gdas1.pl` bezieht sich auf GDAS (Analyse-System, nur kurze Vorläufe). Für die 384-h-Prognosen ist **GFS** (`filter_gfs_0p25.pl` bzw. `filter_gfs_0p25_1hr.pl`) die richtige Quelle. Die dort aufgeführten „GRIB IDs“ (11, 12, 33 …) sind GRIB1-Parameternummern; GRIB2 wird über Kurznamen + Level (`TMP:2 m above ground`) adressiert.

---

## 2. Gesamtbild

### 2.1 Systemkontext

```mermaid
flowchart LR
    subgraph EXT["Internet: kostenfreie Quellen"]
        direction TB
        NWP["Wettermodelle<br/>GFS/GEFS, ECMWF IFS/AIFS,<br/>ICON-EU/D2, GEM"]
        MKT["Markt und Netz<br/>ENTSO-E, SMARD,<br/>Energy-Charts, Netztransparenz"]
        STAT["Stammdaten<br/>MaStR, Feiertage, Geometrien"]
    end

    SYS(["PredictionDB<br/>Docker Compose auf PVE-VM"])

    subgraph USE["Nutzung"]
        direction TB
        DASH["Grafana-Dashboards"]
        API["REST-API<br/>Prognosen, Punktabfragen,<br/>eigene PV-, Wind-, Wasseranlagen"]
    end

    NWP -- "GRIB2, Partial Download" --> SYS
    MKT -- "REST/JSON" --> SYS
    STAT -- "ZIP/XML, quartalsweise" --> SYS
    SYS --> DASH
    SYS --> API
```

### 2.2 Dienste und Datenflüsse

```mermaid
flowchart TB
    subgraph SRC["Externe Quellen"]
        NWP["NOAA, ECMWF, DWD, MSC"]
        EN["ENTSO-E, SMARD, Energy-Charts"]
        MASTR["MaStR-Gesamtexport"]
    end

    subgraph VM["PVE-VM, Docker Compose"]
        direction TB
        subgraph ING["Ingest"]
            WXI["wx-ingest<br/>je Quellfamilie ein Container"]
            ENI["energy-ingest<br/>inkl. mastr-snapshot"]
        end

        subgraph STORE["Speicher"]
            S3[("Garage S3<br/>wx-bronze, wx-silver,<br/>assets-*, ml-artifacts")]
            PG[("PostgreSQL + TimescaleDB<br/>meta, energy, feat,<br/>assets, fcst")]
        end

        WXP["wx-process<br/>GRIB → Zarr → Features"]

        subgraph ML["Machine Learning"]
            TRAIN["ml-train<br/>bei Bedarf"]
            PRED["ml-predict<br/>nach jedem Lauf"]
            MLF["MLflow"]
        end

        subgraph OUT["Ausgabe"]
            APIS["api (FastAPI)"]
            GRAF["Grafana"]
        end

        MAINT["maintenance<br/>Lifecycle, Backups"]
    end

    NWP -- "Range-Requests / Grib-Filter" --> WXI
    EN -- "REST" --> ENI
    MASTR -- "ZIP" --> ENI

    WXI -- "GRIB2 (Bronze)" --> S3
    WXI -- "Laufstatus + NOTIFY" --> PG
    ENI -- "Zeitreihen, Anlagen" --> PG
    ENI -- "Original-ZIP" --> S3

    PG -. "LISTEN nwp_run_complete" .-> WXP
    S3 -- "Bronze lesen" --> WXP
    WXP -- "Zarr (Silber)" --> S3
    WXP -- "Features (Gold)" --> PG

    PG --> TRAIN
    TRAIN -- "Modelle, Metriken" --> MLF
    MLF -- "Artefakte" --> S3
    MLF -- "production-Modell" --> PRED
    PG -. "LISTEN features_ready" .-> PRED
    PRED -- "Quantile" --> PG

    PG --> APIS
    S3 -- "Punktabfragen" --> APIS
    PG --> GRAF
    MAINT --> S3
    MAINT --> PG
```

Grundprinzipien:

1. **Idempotent und wiederaufsetzbar.** Jeder Schritt ist über `(quelle, modell, lauf, schritt, variable)` eindeutig adressiert. Ein Neustart holt nur fehlende Teile nach.
2. **Entkopplung über Zustände in Postgres statt Message-Broker.** Ingest schreibt „Lauf X vollständig in Bronze“, Process pollt bzw. lauscht per `LISTEN/NOTIFY`. Kein RabbitMQ/Redis nötig. Wenn später nötig, lässt sich ein Broker ergänzen.
3. **Rohdaten bleiben erhalten.** Feature-Definitionen ändern sich während der ML-Entwicklung ständig; Bronze erlaubt Neuberechnung ohne erneuten Download (viele Quellen halten Daten nur Stunden bis Tage vor).
4. **Prognose so speichern, wie sie zum Entscheidungszeitpunkt bekannt war** (Lauf + Vorlauf). Sonst entsteht beim Training Daten-Leakage.

---

## 3. Datenquellen (nur kostenfrei)

### 3.1 Numerische Wettermodelle

| Modell | Betreiber | Auflösung | Läufe/Tag | Horizont | Zugang (Primär → Ausweich) | Partial-Download-Mechanik | Lizenz | Vorhaltezeit Quelle |
|---|---|---|---|---|---|---|---|---|
| **GFS** | NOAA/NCEP | 0,25° | 4 (00/06/12/18Z) | **384 h** (stündl. bis 120 h, dann 3-stündl.) | NOMADS Grib-Filter → AWS `noaa-gfs-bdp-pds` → Google Cloud `global-forecast-system` → Azure | NOMADS: serverseitig Variablen + Subregion; Clouds: `.idx`-Datei → HTTP-Range | Public Domain | NOMADS ~10 Tage; AWS Archiv ab 2021 |
| **GEFS** (Ensemble) | NOAA/NCEP | 0,25° / 0,5° | 4 | 384 h (Kontrolle + 30 Member) | NOMADS → AWS `noaa-gefs-pds` | wie GFS | Public Domain | wie GFS |
| **ECMWF IFS (Open Data)** | ECMWF | 0,25° | 4 (00/12Z lang, 06/18Z kurz) | 360 h (00/12Z), 144 h (06/18Z) | `data.ecmwf.int` → AWS `ecmwf-forecasts` → Azure / Google | `.index` (JSON-Zeilen mit Offset/Länge) → HTTP-Range; Python-Paket `ecmwf-opendata` | CC BY 4.0 | Portal wenige Tage; AWS-Archiv länger |
| **ECMWF AIFS** | ECMWF | 0,25° | 4 | 360 h | wie IFS | wie IFS | CC BY 4.0 | wie IFS |
| **ICON-EU** | DWD | 0,0625° (~6,5 km), regulär | 8 | 120 h (00/06/12/18Z), sonst 30 h | `opendata.dwd.de` | eine Datei je Variable/Schritt (bz2) → nur gewünschte Dateien laden | DWD Open Data (Quellenvermerk) | **ca. 24 h** – muss zeitnah gezogen werden |
| **ICON-D2** | DWD | ~2,2 km | 8 | 48 h | `opendata.dwd.de` | wie ICON-EU (zusätzlich `regular-lat-lon`-Variante) | wie oben | ca. 24 h |
| **ICON (global)** | DWD | ~13 km, Ikosaeder | 4 | 180 h (00/12Z) | `opendata.dwd.de` | je Variable; Regridding nötig | wie oben | ca. 24 h |
| **GEM Global** | Environment Canada (MSC) | 15 km / 0,24° | 2 | 240 h | MSC Datamart `dd.weather.gc.ca` | je Variable/Schritt eine Datei | Open Government Licence Canada | ca. 24 h |
| **ARPEGE / AROME** | Météo-France | 0,1° / 0,025° | 4 / 8 | 102 h / 51 h | `portail-api.meteofrance.fr` | Paketweise | Etalab 2.0, **kostenloser API-Schlüssel nötig** | kurz |
| **Open-Meteo** | Open-Meteo (Aggregator) | je Modell | – | bis 16 Tage | `api.open-meteo.com` | Punkt-JSON | CC BY 4.0, **nichtkommerziell frei** | Historical-Forecast-API ab 2021/22 |

Empfehlung zur Reihenfolge der Umsetzung:

1. **GFS** (Kernanforderung, längster Horizont, Archiv auf AWS für Backfill).
2. **ECMWF IFS Open Data** (bestes globales Modell; wichtigster Redundanzpartner).
3. **ICON-EU / ICON-D2** (beste Auflösung für DE in den ersten 2–5 Tagen, wo der Day-Ahead-Preis entsteht).
4. GEFS / AIFS / GEM / ARPEGE als Ergänzung bzw. für Unsicherheits-Features.
5. **Open-Meteo** nur für Prototyping und Backfill der Historie (Historical-Forecast-API), nicht als Produktionsquelle, solange die Nutzung kommerziell werden könnte.

Für das **Training** wird Historie benötigt. Archiv-Optionen:

| Zweck | Quelle | Bemerkung |
|---|---|---|
| Historische GFS-Prognosen | AWS `noaa-gfs-bdp-pds` (ab GFS v16, 2021-03-22 12Z), NCAR GDEX d084001 (2015-01-15 bis 2021-03-22 06Z, CC BY 4.0) | AWS per `.idx`-Range wie im Live-Betrieb; GDEX hat keine `.idx` und nur dreistündliche Schritte bis 240 h, danach zwölfstündlich. Die fehlenden `.idx` baut der Workflow `gfs-idx.yml` aus den Nachrichtenköpfen und legt sie als ORAS-Artefakt `predictiondb-data/gfs-0p25-gdex-idx:v1` ab; ohne passende `.idx` liest `header_scan` die Köpfe per Range (5.5) |
| Historische ECMWF-Open-Data-Prognosen | AWS `ecmwf-forecasts` (ab ~2023) | Umfang vorher prüfen |
| Wetter-„Wahrheit“ | ERA5 über Copernicus CDS (kostenloses Konto), DWD-Stationsdaten (CDC), NOAA ISD/Global Hourly auf AWS als Ausweichquelle (7.9) | für Bias-Korrektur und als Ziel bei Hilfsmodellen |
| Schnell-Backfill Punktdaten | Open-Meteo Historical Forecast API | gut für erste Modelle |

### 3.2 Energie- und Marktdaten (Deutschland, Gebotszone DE-LU)

| Daten | Quelle | Zugang | Auflösung | Lizenz |
|---|---|---|---|---|
| Day-Ahead-Preis DE-LU | ENTSO-E Transparency Platform; SMARD (BNetzA); Energy-Charts (Fraunhofer ISE) | ENTSO-E: kostenloser Token per Registrierung; SMARD/Energy-Charts: offen | 15 min (seit Umstellung der SDAC auf 15-min-Produkte im Okt. 2025), davor 60 min | ENTSO-E-Nutzungsbedingungen; CC BY 4.0 |
| Netzlast ist und Prognose (Day-Ahead) | ENTSO-E; SMARD | wie oben | 15 min | wie oben |
| Erzeugung je Energieträger (ist) | ENTSO-E; SMARD; Energy-Charts | wie oben | 15 min | wie oben |
| Wind-/PV-Prognosen der ÜNB | Netztransparenz.de; ENTSO-E | Netztransparenz: kostenlose OAuth-Registrierung | 15 min | frei |
| Marktwerte nach EEG (Solar, Wind an Land, Wind auf See, Base) | Netztransparenz.de | OAuth-Client (Client-ID/-Secret) | Monat, Jahr | Veröffentlichung der ÜNB |
| Grenzkuppelflüsse, Nachbar-Preise | ENTSO-E | Token | 15/60 min | – |
| Installierte Leistung je Standort | Marktstammdatenregister (MaStR), Gesamtdatenexport | offen | Stichtag | DL-DE BY 2.0 |
| Kraftwerksnichtverfügbarkeiten | ENTSO-E (Unavailability) | Token | Ereignis | – |
| Pegel: Wasserstand und Abfluss | PEGELONLINE (WSV, Bundeswasserstraßen); Landesportale (z. B. GKD Bayern, HVZ Baden-Württemberg, Hochwasserportale der Länder) | offen (REST bzw. Download), Landesportale teils nur Webseiten | 15 min | PEGELONLINE DL-DE Zero 2.0; Landesportale je Portal prüfen |
| Niederschlag beobachtet (Radar) | DWD RADOLAN (z. B. RW stündlich, 1 km) auf opendata.dwd.de | offen | 5 min / 1 h | DWD-Nutzungsbedingungen (frei, Quellenangabe) |
| Abflussprognosen | GloFAS (Copernicus CEMS) über das Early Warning Data Store; Vorhersagen der Landes-Hochwasserzentralen | GloFAS: kostenlose Registrierung | täglich, bis 30 Tage | Copernicus-Lizenz |
| Speicherfüllstände Wasserkraft (Nachbarländer) | ENTSO-E „Water reservoirs and hydro storage plants“ | Token | Woche | – |
| Feiertage, Schulferien | Python `holidays`; Ferien-API bzw. statische Tabellen | offen | Tag | – |

Die Tabelle zeigt die Planung. **Umgesetzt** sind in `services/energy_ingest` SMARD (Rang 1), Energy-Charts (Preise DE-LU und 14 Nachbarzonen, Erzeugung, Last, Prognosen, Grenzhandel `xb_trade_<land>` und physikalische Flüsse `xb_flow_<land>` je Grenze samt Summe `_net`), die ENTSO-E-Day-Ahead-Lastprognose für DE und 11 Nachbarzonen (`entsoe_last`), eigene Marktwerte aus SMARD (`pdb_marktwert`), Brennstoffpreise und Wechselkurs (EZB, Weltbank, EU Oil Bulletin, EIA, EIA STEO), Gasspeicher und LNG (GIE AGSI+/ALSI) und Monatsbilanzen von Eurostat. Unfreie Quellen (ENTSO-E-Preis/Ist-Last/Erzeugung, Netztransparenz, Reuters-Reihen der EIA, THE) laufen nur hinter `PDB_ALLOW_*` (7.11.1). Übersicht mit Lizenzen und Schlüsseln: [DATENQUELLEN-ZUGANG.md](DATENQUELLEN-ZUGANG.md). Pegel, Radar, GloFAS und Wasserkraft-Speicher sind noch nicht angebunden.

**Brennstoff- und CO₂-Preise:** Tägliche Preise für TTF-Gas und EUA sind nicht frei zu bekommen (EEX, ICE und THE untersagen die Weitergabe). Frei sind Monatspreise (Weltbank), Wochenpreise (Oil Bulletin), Tageswerte für Diesel/Gasöl (EIA) und die Monatsprognosen des EIA STEO. Als tägliches Ersatzmaß rechnet `ml-train` einen Grenzkostenindex aus SMARD: den Preis in Stunden hoher Residuallast (Gas, Steinkohle preissetzend) und in Braunkohle-Stunden (folgt dem CO₂-Preis), siehe [PROGNOSEMODELLE.md](PROGNOSEMODELLE.md) 10.2.

### 3.3 Redundanzkonzept

- Je **Modell** mehrere Endpunkte (siehe Tabelle) mit fester Reihenfolge und Circuit-Breaker. Fällt NOMADS aus oder drosselt, geht derselbe Lauf über AWS.
- Je **Zielgröße** mehrere Modelle. Fällt ein Modell ganz aus, rechnet das ML-Modell mit den übrigen weiter (Masking-Features „Quelle verfügbar“, siehe 7.3).
- Je **Energiedatensatz** zwei Quellen (ENTSO-E und SMARD/Energy-Charts) mit Plausibilitätsabgleich.

---

## 4. GRIB-Partial-Download im Detail

### 4.1 Zwei Mechaniken

**a) Serverseitiger Filter (NOMADS Grib-Filter)**

```
https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl
  ?dir=/gfs.20261020/12/atmos
  &file=gfs.t12z.pgrb2.0p25.f042
  &var_TMP=on&var_UGRD=on&var_VGRD=on&var_DSWRF=on&var_TCDC=on
  &lev_2_m_above_ground=on&lev_10_m_above_ground=on&lev_100_m_above_ground=on&lev_surface=on
  &subregion=&toplat=58&bottomlat=44&leftlon=0&rightlon=20
```

Vorteil: kleinste Übertragung (nur Variablen und Ausschnitt). Nachteil: NOMADS drosselt bzw. sperrt IPs bei zu vielen Anfragen pro Minute. Konservatives Rate-Limit einplanen (Größenordnung ≤ 1 Anfrage/s; Grenzwert vor Inbetriebnahme verifizieren) und die Anzahl der Anfragen klein halten (alle Variablen eines Schritts in einer Anfrage).

**b) Index-basierte HTTP-Range-Requests (AWS/GCS/Azure, ECMWF)**

1. `…/gfs.t12z.pgrb2.0p25.f042.idx` laden. Zeilen der Form `<n>:<byte_offset>:d=<lauf>:<VAR>:<LEVEL>:<fcst>:`.
2. Gewünschte Nachrichten per Regex auf `VAR:LEVEL` wählen; Länge = Offset der nächsten Zeile minus eigener Offset.
3. Benachbarte Bereiche zusammenfassen und mit einem Multi-Range- bzw. mehreren parallelen Range-Requests laden.
4. Global geladenes Feld lokal auf die Europa-Box zuschneiden, bevor es nach Bronze geht.

ECMWF Open Data liefert statt `.idx` eine `.index`-Datei mit JSON-Zeilen (`_offset`, `_length`, `param`, `levtype`, `step` …); gleiche Logik.

DWD und MSC bieten pro Variable und Schritt eigene Dateien an; dort ist der „Partial Download“ einfach die Auswahl der Dateien.

Ablauf eines index-basierten Downloads je Prognoseschritt:

```mermaid
sequenceDiagram
    autonumber
    participant I as wx-ingest
    participant Q as Quelle (AWS/GCS/ECMWF)
    participant S3 as Garage (Bronze)
    participant DB as Postgres (meta)

    I->>Q: GET f042.idx
    Q-->>I: Index (Nachricht, Byte-Offset)
    I->>I: VAR:LEVEL filtern,<br/>Bereiche zusammenfassen
    I->>Q: GET f042 mit Range: bytes=a-b, c-d, ...
    Q-->>I: GRIB2-Nachrichten (global)
    I->>I: auf 44–58° N, 0–20° O zuschneiden
    I->>S3: PUT gfs/0p25/…/f042.grib2
    I->>DB: grib_message → stored (SHA-256, Größe)
```

### 4.2 Inventarisierung und Vollständigkeit

Für jeden Lauf wird erwartet, welche Nachrichten kommen (aus der Quellen-Konfiguration abgeleitet). Tabelle `meta.grib_message` (Kap. 5.3) führt Status je Nachricht: `pending → downloaded → stored → processed`. Ein Lauf gilt als vollständig, wenn alle erwarteten Nachrichten mindestens `stored` sind. Verspätete Schritte (GFS veröffentlicht über ca. 1,5 h hinweg schrittweise) werden durch periodisches Nachpollen geholt; erst danach wird der Lauf an `wx-process` übergeben (Teilübergabe für frühe Schritte optional).

Zustände eines Laufs (`meta.nwp_run.status`):

```mermaid
stateDiagram-v2
    [*] --> discovered: Lauf erwartet (Zeitplan)
    discovered --> downloading: erste .idx/.index verfügbar
    downloading --> downloading: weitere Schritte veröffentlicht
    downloading --> complete: alle Nachrichten stored
    downloading --> failed: Frist überschritten,<br/>alle Endpunkte erschöpft
    failed --> downloading: Nachholen (Backfill/Retry)
    complete --> processed: wx-process fertig<br/>(Silber + Gold)
    processed --> [*]
```

Zustände einer einzelnen Nachricht (`meta.grib_message.status`):

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> downloaded: Range-Request ok
    downloaded --> stored: Zuschnitt + SHA-256<br/>+ Upload Bronze
    stored --> processed: in Zarr übernommen
    pending --> pending: Fehler, Backoff,<br/>nächster Endpunkt
```

### 4.3 Vorschlag Variablenumfang (Start)

| Zweck | GFS-Kurzname : Level | ECMWF `param` | ICON |
|---|---|---|---|
| Temperatur (Last: Heizen/Kühlen) | `TMP:2 m above ground` | `2t` | `t_2m` |
| Taupunkt/Feuchte (auch Kühllast, 7.8) | `DPT:2 m above ground` / `RH:2 m` | `2d` | `td_2m` |
| Wind 10 m | `UGRD/VGRD:10 m above ground` | `10u/10v` | `u_10m/v_10m` |
| Wind 100 m (Nabenhöhe) | `UGRD/VGRD:100 m above ground` | `100u/100v` | (über Modellflächen; vorerst 10 m + Böen) |
| Böen | `GUST:surface` | `10fg` | `vmax_10m` |
| Globalstrahlung | `DSWRF:surface` | `ssrd` (akkumuliert) | `aswdir_s` + `aswdifd_s` |
| Bewölkung | `TCDC:entire atmosphere` | `tcc` | `clct` |
| Niederschlag / Schnee | `APCP`, `CSNOW` | `tp`, `sf` | `tot_prec` |
| Schneedecke (Wasseräquivalent, Wasserkraft 7.6) | `WEASD:surface`, `SNOD:surface` | `sd` | `w_snow`, `h_snow` |
| Bodenfeuchte (Wasserkraft 7.6) | `SOILW:0-0.1 m below ground`, `SOILW:0.1-0.4 m below ground` | `swvl1`–`swvl4` | `w_so` |
| Bodentemperatur (Sole-Wärmepumpen, Netzverluste, 7.8) | `TSOIL:0-0.1 m below ground`, `TSOIL:0.1-0.4 m below ground` | `stl1`, `stl2` | `t_so` |
| Langwellige Gegenstrahlung (Gebäude, 7.8) | `DLWRF:surface` | `strd` | `athb_s` (Netto) |
| Sonnenscheindauer (7.8) | `SUNSD:surface` | – (aus Strahlung ableiten) | `dursun` |
| Bodendruck | `PRMSL:mean sea level` | `msl` | `pmsl` |
| Druck am Boden (Luftdichte für Wind 7.6, Enthalpie 7.8) | `PRES:surface` | `sp` | `ps` |

Alle GFS-Felder dieser Tabelle stehen in `config/sources/gfs_0p25.yaml` (22 Felder) und werden seit 2026-10-21 gesammelt. Die ECMWF- und ICON-Spalten gelten, sobald diese Quellen dazukommen; die Namen dort vor der Umsetzung prüfen.

Ausschnitt: **44–58° N, 0–20° E** (DE plus Nachbarn, relevant für Kuppelflüsse und Wetterlagen). Bei GFS 0,25° sind das 57 × 81 = 4 617 Gitterpunkte.

---

## 5. Speicherung

### 5.1 Drei Stufen

| Stufe | Format | Ort | Inhalt | Zweck |
|---|---|---|---|---|
| **Bronze** | GRIB2 (Original-Codierung, zugeschnitten) | Garage `wx-bronze` | eine Datei je `modell/lauf/schritt` | Unveränderliche Quelle, Neuberechnung, Audit |
| **Silber** | Zarr v3, je Variable `(schritt, lat, lon)`, Int16 mit Scale/Offset, Blosc/Zstd mit Shuffle | Garage `wx-silver` | ein Zarr-Store je Modell und Lauf | Schneller Zugriff für Training, Regridding auf gemeinsames Gitter |
| **Gold** | TimescaleDB-Hypertables, native Kompression | Postgres | Features je Region × Lauf × Gültigkeitszeit, Energiedaten, Prognosen | ML-Eingang, API, Grafana |

```mermaid
flowchart LR
    RAW["Quelle<br/>globale GRIB2-Datei"] -- "nur benötigte<br/>Nachrichten" --> B
    B[("Bronze<br/>GRIB2, zugeschnitten<br/>Garage wx-bronze")] -- "dekodieren,<br/>auf Zielgitter" --> SI
    SI[("Silber<br/>Zarr, float16 + Zstd<br/>Garage wx-silver")] -- "Gewichte je Region<br/>bzw. Anlagen-Cluster" --> G
    G[("Gold<br/>Features je Region × Lauf × Zeit<br/>TimescaleDB feat.*")] --> ML["Training / Inferenz"]
    SI -- "Punktabfrage" --> API["api"]
```

Umgesetzt in `services/wx_process` (Revision `0003_wx_features`): Silber liegt als ein Store je Lauf unter `wx-silver/gfs/0p25/JJJJ/MM/TT/HH.zarr` (statt eines wachsenden Stores je Modell), damit Neuberechnung und Aufräumen auf einen Lauf beschränkt bleiben; Int16 mit CF-Attributen `scale_factor`/`add_offset`, lesbar mit `xarray.open_zarr`. Mittel und Summen stehen dort als Werte für das Intervall seit dem vorherigen Schritt (die 6-h-Eimer von GFS sind zurückgerechnet). Einen vollständigen Lauf erkennt `wx-process` vorerst am Laufmanifest `run.json` in Bronze (Abfrage alle 10 min), nicht über `NOTIFY nwp_run_complete`; danach setzt es `meta.nwp_run` auf `processed` und sendet `NOTIFY features_ready`.

**Zeitachse:** Bronze und Silber halten die Zeitschritte der Quelle unverändert (GFS: stündlich bis 120 h, danach dreistündlich). Zeitliche Interpolation geschieht erst beim Lesen und nur auf Anforderung (7.9).

Objekt-Schlüssel (Bronze), sprechend und partitionierbar:

```
wx-bronze/gfs/0p25/2026/10/20/12/f042.grib2
wx-bronze/ecmwf-ifs/0p25/2026/10/20/00/s144.grib2
wx-bronze/icon-eu/2026/10/20/06/t_2m/s017.grib2
```

### 5.2 Garage (Single Node)

- Image `dxflrs/garage`, `replication_factor = 1`, ein Datenverzeichnis und ein Metadatenverzeichnis auf getrennten Volumes (Metadaten möglichst auf SSD).
- Buckets: `wx-bronze`, `wx-silver`, `ml-artifacts` (MLflow), `ml-exchange` (Trainings-Snapshots und -Ergebnisse, 7.7), `backups` (pg_dump/WAL-G).
- Zugriff ausschließlich über S3-API (`obstore` oder `s3fs`/`fsspec`), Endpoint, Region und Keys aus Umgebungsvariablen. Damit ist der Wechsel zu jedem S3-kompatiblen Dienst reine Konfiguration.
- Lebenszyklus: Bronze für GFS z. B. 2 Jahre vollständig, danach nur 00Z/12Z behalten; Silber unbegrenzt (klein). Garage unterstützt Lifecycle-Regeln nur eingeschränkt, daher Aufräumjob im `maintenance`-Container.
- Hinweis: Single Node heißt **keine Redundanz auf Speicherebene**. Backups der Buckets auf ein zweites Ziel (anderer PVE-Storage oder externes S3) über `rclone` einplanen, sobald Daten wertvoll werden (z. B. weil Quellen sie nicht mehr vorhalten).

### 5.3 Datenbank: PostgreSQL + TimescaleDB statt MariaDB

Begründung gegenüber dem PredictionDB-Entwurf:

- **Native spaltenorientierte Kompression** der Hypertables (typisch 90–95 % Ersparnis bei Zeitreihen). MariaDB bietet nichts Vergleichbares ohne Zusatz-Engine.
- Continuous Aggregates (z. B. 15 min → Stunde/Tag) und Retention-Policies eingebaut; ersetzt den eigenen Cleanup-Container.
- `LISTEN/NOTIFY` für die Entkopplung der Dienste.
- Gute Cloud-Optionen (Timescale Cloud, oder Managed Postgres mit Timescale-Apache-Variante bzw. Self-Hosting auf k8s).

Schemas (Auszug):

```mermaid
erDiagram
    SOURCE ||--o{ NWP_RUN : "liefert"
    NWP_RUN ||--o{ GRIB_MESSAGE : "besteht aus"
    SOURCE ||--o{ FEATURE : "definiert"
    FEATURE ||--o{ NWP_VALUE : "Werte"
    REGION ||--o{ NWP_VALUE : "für"
    SOURCE ||--o{ SERIES : "liefert"
    SERIES ||--o{ ENERGY_VALUE : "Werte"

    SOURCE {
        smallint source_id PK
        text code
        text kind
    }
    NWP_RUN {
        smallint source_id PK
        timestamptz run_time PK
        text status
        text endpoint
    }
    GRIB_MESSAGE {
        timestamptz run_time PK
        smallint step_h PK
        text var PK
        text level PK
        text status
        text object_key
    }
    FEATURE {
        int feature_id PK
        text code
        text aggregation
        text unit
    }
    REGION {
        smallint region_id PK
        text code
    }
    NWP_VALUE {
        int feature_id PK
        smallint region_id PK
        timestamptz run_time PK
        timestamptz valid_time PK
        real value
    }
    SERIES {
        int series_id PK
        text code
        interval resolution
    }
    ENERGY_VALUE {
        int series_id PK
        timestamptz ts PK
        float8 value
        timestamptz published
    }
    PREDICTION {
        text model_version PK
        text target PK
        timestamptz issue_time PK
        timestamptz valid_time PK
        real quantile PK
        float8 value
    }
```


```sql
-- meta: was wurde wann woher geladen
CREATE TABLE meta.source (
  source_id    smallserial PRIMARY KEY,
  code         text UNIQUE NOT NULL,      -- 'gfs_0p25', 'ecmwf_ifs', 'icon_eu', 'entsoe', ...
  kind         text NOT NULL,             -- 'nwp' | 'energy' | 'static'
  license      text
);

CREATE TABLE meta.nwp_run (
  source_id    smallint REFERENCES meta.source,
  run_time     timestamptz NOT NULL,
  status       text NOT NULL,             -- 'discovered'|'downloading'|'complete'|'processed'|'failed'
  endpoint     text,                      -- tatsächlich genutzter Endpunkt (Redundanz nachvollziehbar)
  n_expected   int, n_stored int,
  bytes_stored bigint,
  updated_at   timestamptz DEFAULT now(),
  PRIMARY KEY (source_id, run_time)
);

CREATE TABLE meta.grib_message (
  source_id smallint, run_time timestamptz, step_h smallint,
  var text, level text,
  status text NOT NULL, object_key text, sha256 bytea, size_bytes int,
  PRIMARY KEY (source_id, run_time, step_h, var, level)
);

-- Register der Merkmale (übernimmt die PDID-Idee aus PredictionDB)
CREATE TABLE meta.feature (
  feature_id   serial PRIMARY KEY,
  source_id    smallint REFERENCES meta.source,
  code         text NOT NULL,             -- 'temperature_2m', 'wind_power_index_100m', ...
  aggregation  text NOT NULL,             -- 'pop_weighted', 'wind_cap_weighted', 'pv_cap_weighted', 'area_mean'
  unit         text NOT NULL,
  UNIQUE (source_id, code, aggregation)
);

CREATE TABLE meta.region (
  region_id smallserial PRIMARY KEY,
  code text UNIQUE NOT NULL               -- 'DE', 'DE-50Hertz', 'DE-Amprion', 'DE-TenneT', 'DE-TransnetBW', 'DE-BY', ...
);

-- feat: Wetter-Features (Gold). Lead-Time explizit, wie in PredictionDB (offset)
CREATE TABLE feat.nwp_value (
  feature_id int      NOT NULL,
  region_id  smallint NOT NULL,
  run_time   timestamptz NOT NULL,
  valid_time timestamptz NOT NULL,
  value      real NOT NULL,
  PRIMARY KEY (feature_id, region_id, run_time, valid_time)
);
SELECT create_hypertable('feat.nwp_value', 'valid_time', chunk_time_interval => interval '7 days');
ALTER TABLE feat.nwp_value SET (timescaledb.compress,
  timescaledb.compress_segmentby = 'feature_id, region_id',
  timescaledb.compress_orderby   = 'run_time, valid_time');

-- energy: Ist-Werte und offizielle Prognosen
CREATE TABLE energy.series (
  series_id serial PRIMARY KEY,
  source_id smallint, code text, unit text, resolution interval,
  UNIQUE (source_id, code)
);   -- 'da_price_de_lu', 'load_actual', 'load_da_forecast', 'load_wa_forecast_min', 'gen_wind_onshore', ...

CREATE TABLE energy.value (
  series_id  int NOT NULL,
  ts         timestamptz NOT NULL,
  value      double precision,
  published  timestamptz,                 -- wann war der Wert bekannt (Leakage-Schutz)
  PRIMARY KEY (series_id, ts)
);
SELECT create_hypertable('energy.value', 'ts');

-- fcst: eigene Prognosen, je Modellversion und Quantil
CREATE TABLE fcst.prediction (
  model_version text, target text, issue_time timestamptz, valid_time timestamptz,
  quantile real, value double precision,
  PRIMARY KEY (model_version, target, issue_time, valid_time, quantile)
);
SELECT create_hypertable('fcst.prediction', 'valid_time');
```

Unterschied zu PredictionDB: Statt `(lat, lon, timestamp, offset)` steht hier `(region, run_time, valid_time)`. `run_time` ist eindeutig und erspart Rückrechnungen; die Vorlaufzeit ist `valid_time − run_time`. Messungen landen nicht mit negativem Offset in derselben Tabelle, sondern in `energy.value` bzw. einer eigenen `obs`-Tabelle. Das hält die Gitter-/Feature-Tabelle homogen und klein.

### 5.4 Mengengerüst

GFS, Ausschnitt 4 617 Punkte, 10 Variablen, 209 Schritte (0–120 h stündlich = 121, 123–384 h dreistündlich = 88), 4 Läufe/Tag:

| Ablage | Rechnung | pro Jahr |
|---|---|---|
| Zeilen je Gitterwert in SQL (PredictionDB-Ansatz) | 4 617 × 10 × 209 × 4 × 365 ≈ **14 Mrd. Zeilen**; bei ~40–60 B/Zeile inkl. Index | **~600–850 GB** (nur GFS) |
| Bronze GRIB2 (zugeschnitten, GRIB-Packing ~1–1,5 B/Wert) | 9,6 Mio. Werte/Lauf ≈ 10–15 MB/Lauf | **~15–22 GB** |
| Silber Zarr float16 + Zstd (~1 B/Wert) | ≈ 8–10 MB/Lauf | **~12–15 GB** |
| Gold Features (z. B. 40 Features × 10 Regionen × 209 Schritte × 4 Läufe) | 120 Mio. Zeilen, komprimiert ~2–4 B/Wert | **~0,5 GB** |

Mit ECMWF, ICON-EU (höhere Auflösung, aber kürzerer Horizont) und GEFS-Mittel/Streuung liegt die Gesamtmenge realistisch bei **100–200 GB pro Jahr**. Eine VM-Platte von 500 GB reicht für die ersten zwei Jahre. Die Werte sind Abschätzungen und werden nach dem ersten Betriebsmonat durch Messung ersetzt (`meta.nwp_run.bytes_stored`).

### 5.5 Datenaustausch zwischen Instanzen über GHCR (geplant)

> **Stand der Umsetzung:** Die Historie baut der Workflow `gfs-history.yml` im
> GitHub-Actions-Runner (`services/wx_exchange`), eine Instanz holt sie mit `wx-import`
> (Compose-Profil `history`). Abweichend vom Entwurf unten gibt es ein Manifest je Monat **und
> Laufzeit** (Tag `JJJJ-MM-HHz`), damit jeder Matrix-Job genau ein Manifest schreibt; der Stand
> steht in den Manifest-Annotationen (`fingerprint`, `content`, `missing`), die Tag-Liste ersetzt
> den Index.
>
> - **Quellen:** `source: aws` für GFS v16 ab 2021-03-22 12Z, `source: gdex` für ältere Läufe aus
>   NCAR GDEX d084001 (bis 2015-01-15). Beide landen im selben Artefakt; `GFS_HISTORY_START` vor
>   2021-03-22 holt auch die GDEX-Läufe.
> - **`.idx` zu GDEX:** Der Workflow `gfs-idx.yml` baut für jede GDEX-Datei eine vollständige
>   `.idx` (Format `wgrib2 -s`) und legt sie als ein Artefakt
>   `predictiondb-data/gfs-0p25-gdex-idx:v1` mit einem Layer je Monat und Laufzeit ab. `publish`
>   liest damit nur die gewählten Nachrichten per Range, statt die Köpfe abzulaufen.
> - **Bestand:** `wx-import` und `wx-process` melden lokales Silber nach `meta.silver_run`; das
>   Dashboard „Wetter“ zeigt daraus Bronze, Silber und Gold je Monat.
> - **Prüfung:** `wx-exchange gfs verify` vergleicht selbst gebautes Silber über den Inhalts-Hash
>   mit GHCR.
>
> Noch offen sind `wx-publish` für den laufenden Betrieb, das Bronze-Artefakt und die
> `cosign`-Signatur. Einzelheiten: `services/wx_exchange/README.md`.

Eine neue Instanz müsste für die GFS-Historie ab 2021-03 heute rund 8 000 Läufe selbst laden: je Lauf etwa 3,3 GB Transfer und 3 300 Range-Anfragen, zusammen rund 27 TB und 27 Mio. Anfragen gegen AWS. Die verdichteten Silber-Stores derselben Läufe sind zusammen nur gut 100 GB groß. Deshalb veröffentlicht die Hauptinstanz ihre verdichteten Wetterdaten als **OCI-Artefakte in der GitHub Container Registry** (`ghcr.io/the78mole/predictiondb-data/…`), und andere Instanzen importieren sie dort, statt GFS selbst herunterzuladen.

#### 5.5.1 Datenqualität ohne Bronze

Silber ist kein vergröbertes Abbild, sondern dasselbe native 0,25°-Gitter mit allen Variablen aus `wx_process.variables.VARIABLES`, ohne räumliche Interpolation und mit den Zeitschritten der Quelle. Alles Nachgelagerte wird aus Silber gerechnet. Eine Instanz, die nur Silber importiert, verliert daher für den laufenden Betrieb nichts:

| Aufgabe | braucht | ohne Bronze möglich |
|---|---|---|
| Features je Cluster, neue Clusterung, neue `FEATURES_VERSION`, neuer MaStR-Stand | Silber | ja |
| Zeitliche Interpolation beim Abruf, Analyse-Jobs (7.9), Punktabfragen und Anlagenprognose (7.6) | Silber | ja |
| Training und Inferenz | Gold, aus Silber gerechnet | ja |
| Neue Silber-Kodierung (`SILVER_VERSION`), z. B. feinere Quantisierung, anderer Wertebereich, Korrektur der Eimer-Rückrechnung | Bronze | nein, neues Silber-Release des Herausgebers nötig |
| Variable, die in Bronze liegt, aber nicht in `VARIABLES` steht | Bronze | nein |
| Abgleich mit dem Original (Audit, Fehlersuche im Decoder) | Bronze | nein |
| Variable, die gar nicht geladen wurde | Quelle | bei keiner Instanz, nur Download aus dem AWS-Archiv |

Zur Quantisierung: Silber speichert Int16 mit festen Stufen (0,01 K, 0,01 m/s, 0,05 W/m², 1 Pa, 0,001 mm/h). Das liegt in der Größenordnung der Packungsgenauigkeit von GFS selbst und weit unter dem Prognosefehler; für die Modelle ist das kein messbarer Verlust (Vorbehalt: an echten Bronze-Dateien gegenprüfen, Aufgabe in 11). Grenzwerte werden abgeschnitten, z. B. Niederschlag oberhalb von rund 62 mm/h.

Ein fehlendes Bronze ist nicht endgültig: Das AWS-Archiv hält GFS v16 ab 2021-03-22 vollständig vor, einzelne Läufe lassen sich jederzeit nachladen (`wx-ingest gfs run`), und der Herausgeber behält Bronze ohnehin. Für Instanzen, die selbst Silber neu bauen wollen, wird Bronze zusätzlich als eigenes, optionales Artefakt veröffentlicht (5.5.2).

#### 5.5.2 Was veröffentlicht wird

| Artefakt-Repository | Inhalt | Größe | Stufe |
|---|---|---|---|
| `predictiondb-data/gfs-0p25-silver` | Zarr-Store je Lauf, unverändert aus `wx-silver` | ~13 MB/Lauf, ~19 GB/Jahr | Pflicht |
| `predictiondb-data/gfs-0p25-bronze` | zugeschnittene GRIB2 je Schritt plus `run.json`, `fSSS.json` | ~22 MB/Lauf, ~32 GB/Jahr | optional, später |
| `predictiondb-data/feat-gfs` | Gold-Features als Parquet je Monat, nur zusammen mit `clustering_version` und `FEATURES_VERSION` | < 1 GB/Jahr | optional; nur nutzbar bei identischer Clusterung |
| `predictiondb-data/gfs-0p25-gdex-idx:v1` | vollständige `.idx` zu allen GDEX-Dateien, ein Layer (`tar.gz`) je Monat und Laufzeit | einige hundert MB | umgesetzt; Hilfsmittel für den GDEX-Backfill |

Nicht veröffentlicht werden Energie- und Marktdaten (ENTSO-E, SMARD, Energy-Charts, Netztransparenz): Sie sind klein, jede Instanz ruft sie per API selbst ab, und ihre Nutzungsbedingungen regeln die Weitergabe unterschiedlich. Ebenso wenig Prognosen und Modelle (Lizenz, 12.1), Geheimnisse oder Inhalte von `assets.*` zu Anlagen von Nutzern.

#### 5.5.3 Format und Adressierung

- **Werkzeug:** ORAS (`oras` CLI bzw. `oras-py`), kein Container-Image. Jedes Artefakt ist ein OCI-Manifest mit eigenem `artifactType`, Layer sind normale Blobs.
- **Ein Manifest je Monat, ein Layer je Lauf.** Tag `JJJJ-MM` (z. B. `2026-10`). Ein Layer ist das Tar des Zarr-Stores `HH.zarr` eines Laufs, ohne weitere Kompression (die Chunks sind schon Zstd). Bei 4 Läufen am Tag hat ein Monat bis zu 124 Layer.
- **Medientypen:** `artifactType: application/vnd.predictiondb.wx-silver.v1`, Layer `application/vnd.predictiondb.zarr.v3.tar`. Bronze analog `…wx-bronze.v1` mit einem Tar je Lauf.
- **Annotationen je Layer:** `org.predictiondb.run` (ISO-Zeit des Laufs), `org.predictiondb.model` (`gfs`), `org.predictiondb.grid` (`0p25`), `org.predictiondb.bbox`, `org.predictiondb.silver_version`, Anzahl der Schritte und Variablen, Hash des Bronze-Laufmanifests. Damit kann ein Importeur einzelne Läufe gezielt ziehen (`oras pull` mit Layer-Filter bzw. Blob-Abruf per Digest), ohne den Monat ganz zu laden.
- **Annotationen je Manifest:** `org.opencontainers.image.source` (Repo), `org.opencontainers.image.created`, `org.opencontainers.image.licenses` (`CC-BY-4.0`, 12.9), `org.predictiondb.attribution` (Quellenvermerk, 5.5.6), Liste der enthaltenen Läufe.
- **Integrität:** Layer sind über ihren SHA-256-Digest adressiert. Zusätzlich signiert der Herausgeber jedes Manifest mit `cosign`; der Import prüft die Signatur, bevor er schreibt.

#### 5.5.4 Versionierung und Inkremente

- **Laufender Monat:** Nach jedem verarbeiteten Lauf schiebt `wx-publish` ein neues Monatsmanifest mit dem zusätzlichen Layer. Bereits hochgeladene Layer werden nicht erneut übertragen (inhaltsadressiert, die Registry kennt den Blob), das Inkrement ist also ein Lauf von ~13 MB. Der Tag `JJJJ-MM` wandert mit; jede Fassung bleibt über ihren Digest abrufbar.
- **Abgeschlossener Monat:** Sind alle Läufe da (oder als endgültig fehlend markiert), bekommt das Manifest zusätzlich den unveränderlichen Tag `JJJJ-MM.final`. Fehlende Läufe stehen in einer Manifest-Annotation, damit Importeure sie nicht als Lücke suchen.
- **Formatwechsel:** Eine neue `SILVER_VERSION` erzeugt ein neues Artefakt-Repository mit Suffix (`gfs-0p25-silver-v2`), das der Herausgeber aus seinem Bronze neu aufbaut. Alte Versionen bleiben stehen; ein Importeur entscheidet über die Konfiguration, welche Version er bezieht. Ein Lauf darf in einer Instanz nur in einer Silber-Version liegen.
- **Index:** Der Tag `index` zeigt auf ein kleines JSON-Artefakt mit allen Monaten, ihren Digests und dem Stand. Ein Importeur liest nur den Index und lädt die Monate nach, deren Digest sich geändert hat.
- **Nichts wird gelöscht.** Fehlerhafte Läufe werden durch ein neues Manifest ohne bzw. mit korrigiertem Layer ersetzt, alte Digests bleiben gültig.

#### 5.5.5 Ablauf

```mermaid
flowchart LR
    subgraph PUB["Hauptinstanz"]
        S1[("wx-silver")] --> WP["wx-publish<br/>nach features_ready"]
        B1[("wx-bronze")] -. optional .-> WP
    end
    WP -- "oras push<br/>+ cosign sign" --> GHCR[("ghcr.io/the78mole/<br/>predictiondb-data/*")]
    subgraph IMP["Weitere Instanz"]
        WI["wx-import<br/>Index lesen, fehlende Läufe ziehen"] --> S2[("wx-silver")]
        WI --> M[("meta.nwp_run<br/>origin = ghcr")]
        S2 --> WPR["wx-process<br/>nur Gold, lokale Clusterung"]
        WI -. "Lauf fehlt nach Frist" .-> WXI["wx-ingest<br/>eigener Download"]
    end
    GHCR --> WI
```

- **Herausgeber (`wx-publish`, neuer Compose-Dienst):** Lauscht auf `features_ready` bzw. pollt `meta.nwp_run`, packt den fertigen Silber-Store, schiebt den Layer und das neue Monatsmanifest und setzt `meta.nwp_run.published_at`. Er läuft auf der VM, weil dort die Daten liegen; das Schreibrecht auf GHCR kommt aus einem Token mit `write:packages` aus dem Secret-Store (8.3). Ein `backfill`-Modus veröffentlicht die Historie monatsweise.
- **Importeur (`wx-import`, neuer Modus von `wx-ingest`):** Quelle `ghcr` in `config/sources/gfs_0p25.yaml` vor `nomads`/`aws`. Liest den Index, zieht fehlende Läufe, prüft Signatur und Digests, schreibt den Store nach `wx-silver` und setzt `meta.nwp_run` auf `silver_ready` mit `origin = ghcr`. `wx-process` rechnet danach wie gewohnt nur die Gold-Features, mit der lokalen Clusterung. Bronze bleibt leer; Jobs, die Bronze brauchen, erkennen das am Ursprung und überspringen den Lauf oder laden ihn gezielt nach.
- **Aktualität:** Der Herausgeber veröffentlicht einen Lauf erst nach der Verarbeitung, also gut 5,5 h nach Laufzeit (GFS f384 kommt nach ~5:15 h). Für Backfill und Training spielt das keine Rolle. Für den Live-Betrieb einer importierenden Instanz gilt eine Frist (Vorschlag 7 h nach Laufzeit): Ist der Lauf bis dahin nicht in GHCR, lädt sie ihn selbst. Wer Day-Ahead mit dem 06Z-Teillauf rechnen will (3.1, Analyse `gfs-folgelauf`), lädt diesen Teil weiterhin selbst.

#### 5.5.6 Grenzen, Kosten und Lizenz

- **GHCR:** Für öffentliche Pakete berechnet GitHub weder Speicher noch Transfer. Ein Layer darf höchstens 10 GB groß sein, ein Upload darf höchstens 10 min dauern; mit einem Lauf je Layer (~13 bzw. ~22 MB) liegt das weit darunter. Die Pakete werden mit dem öffentlichen Repo verknüpft und erben dessen Sichtbarkeit, Abruf geht anonym. Vor der Umsetzung prüfen: aktuelle Grenzen und eine etwaige Fair-Use-Regel für Datenmengen im dreistelligen GB-Bereich.
- **Mengen:** Silber ab 2021-03 ≈ 105 GB, Bronze ≈ 180 GB (Stand 2026-10, je mit 4 Läufen/Tag), danach ~19 bzw. ~32 GB/Jahr.
- **Quellen und Weitergabe:**

| Quelle | Weitergabe | Auflage |
|---|---|---|
| NOAA GFS/GEFS | erlaubt, gemeinfrei (Werk der US-Regierung) | keine Pflicht; Quellenvermerk „NOAA/NCEP GFS“ wird mitgeführt |
| ECMWF Open Data (IFS/AIFS) | erlaubt, CC BY 4.0 | Quellenvermerk und Hinweis auf Veränderung |
| DWD Open Data (ICON) | erlaubt | Quellenvermerk „Deutscher Wetterdienst“ (GeoNutzV), Hinweis auf Veränderung |
| MSC GEM | vor Aufnahme prüfen (Open Government Licence – Canada) | Quellenvermerk |
| Open-Meteo, Energie- und Marktdaten | nicht vorgesehen | — |

  Jedes Manifest trägt den Quellenvermerk in `org.predictiondb.attribution`, zusätzlich liegt ein `ATTRIBUTION.md` als eigener Layer im Index-Artefakt. Weitere Modelle bekommen eigene Artefakt-Repositories (`ecmwf-ifs-0p25-silver` usw.), damit Lizenzen nicht gemischt werden.
- **Lizenz der Daten selbst:** Die BSL 1.1 gilt für den Code. Die veröffentlichten Daten stehen unter **CC BY 4.0** (`org.opencontainers.image.licenses: CC-BY-4.0`), Namensnennung: „PredictionDB (the78mole)“ plus die Quellenvermerke der Tabelle oben (12.9).

---

## 6. Software-Struktur (Python, uv)

### 6.1 Repository-Layout (Vorschlag, ein Monorepo als uv-Workspace)

```
PredictionDB/
├── pyproject.toml              # [tool.uv.workspace] members = ["packages/*", "services/*"]
├── uv.lock
├── compose.yaml                # + compose.override.yaml (dev), compose.prod.yaml
├── .env.example
├── config/
│   ├── sources/                # eine YAML je Quelle (Endpunkte, Variablen, Zeitplan, Ausschnitt)
│   │   ├── gfs_0p25.yaml
│   │   ├── ecmwf_ifs.yaml
│   │   ├── icon_eu.yaml
│   │   └── ...
│   ├── regions/                # GeoJSON/Masken für DE, Regelzonen, Bundesländer
│   └── garage.toml
├── packages/
│   ├── core/                   # Konfiguration (pydantic-settings), Logging, S3-Client, DB-Zugriff, Modelle
│   ├── gribkit/                # .idx/.index-Parser, Range-Merging, Zuschnitt, GRIB→xarray (eccodes)
│   └── features/               # Regionen-Aggregation, Wind-/PV-Proxies, Kalendermerkmale
├── services/
│   ├── wx_ingest/              # Quellen-Plugins + Scheduler
│   ├── wx_process/             # Bronze → Silber → Gold
│   ├── energy_ingest/          # ENTSO-E, SMARD, Energy-Charts, Netztransparenz, MaStR
│   ├── ml/                     # Datasets, Modelle, Training, Evaluierung, Inferenz
│   └── api/                    # FastAPI
├── migrations/                 # Alembic (SQL inkl. Timescale-Befehle)
├── docker/                     # ein gemeinsames Multi-Stage-Dockerfile mit Targets je Dienst
└── tests/                      # inkl. aufgezeichneter .idx-/GRIB-Beispiele als Fixtures
```

**Umsetzungsstand:** Pakete `packages/core` (`predictiondb_core`) und `packages/gribkit`; Dienste `services/wx_ingest`, `services/wx_process`, `services/wx_exchange` (GHCR-Austausch, 5.5), `services/energy_ingest`, `services/mastr_ingest` (MaStR getrennt vom Energie-Ingest) und `services/ml_train` (Training und Inferenz); dazu der schlanke Prognose-Stack `predict/` (7.11). Konfiguration unter `config/sources/`, `config/process/`, `config/ml/`, `config/grafana/` und `config/prometheus/`. Noch nicht vorhanden sind `packages/features`, `services/api` und `config/regions/`.

### 6.2 Bibliotheken

| Zweck | Wahl | Bemerkung |
|---|---|---|
| HTTP | `httpx` (async, HTTP/2) + `tenacity` (Retry/Backoff) | Range-Requests, Rate-Limit über `aiolimiter` |
| GRIB lesen/schreiben | `eccodes` (Python-Wheels bringen die Bibliothek mit) + `cfgrib`/`xarray` | GRIB-Zuschnitt ohne Dekodieren via eccodes-`sub_area`-Logik bzw. dekodieren → neu packen |
| Referenz/Abkürzung | **Herbie** (`herbie-data`) | Implementiert genau den `.idx`-Partial-Download für GFS, GEFS, ECMWF, ICON, HRRR. Entweder direkt nutzen oder als Referenz für `gribkit`. Empfehlung: im Spike prüfen, ob es unsere Anforderungen (Ausweich-Endpunkte, Zustandsführung) ohne Umbauten trägt. |
| ECMWF | `ecmwf-opendata` | offizieller Client |
| Gitter/Regridding | `xarray`, `numpy`, `xesmf` oder einfache bilineare Interpolation | ICON-Ikosaeder nur bei Bedarf |
| Zarr | `zarr` v3 + `obstore`/`s3fs` | |
| DB | `SQLAlchemy 2` + `psycopg 3`, `alembic` | Bulk-Insert per `COPY` |
| Konfiguration | `pydantic-settings` + YAML | Umgebungsvariablen überschreiben YAML |
| Scheduling | `APScheduler 4` (in-process, Cron-Trigger, Persistenz in Postgres) | kein Celery/Redis nötig |
| Energie-APIs | `entsoe-py`; SMARD/Energy-Charts über eigene dünne Clients | |
| ML | PyTorch + Lightning; `neuralforecast` für Referenzarchitekturen; LightGBM als Baseline | |
| Anlagenmodelle (7.6) | `pvlib` (PV: Sonnenstand, Transposition, Zelltemperatur), `windpowerlib` (Höhenprofil, Leistungskurven) | beide BSD-3-Clause bzw. MIT |
| Tracking | MLflow (Backend Postgres, Artefakte in Garage) | |
| Observability | `structlog` (JSON), Prometheus-Metriken via `prometheus-client` | |

### 6.3 Plugin-Schnittstelle (Quelle)

```python
class NwpSource(Protocol):
    code: str

    def expected_runs(self, now: datetime) -> list[datetime]: ...
    def expected_messages(self, run: datetime) -> list[MessageSpec]: ...   # (step, var, level)
    async def is_available(self, run: datetime, step: int, ep: Endpoint) -> bool: ...
    async def fetch(self, run: datetime, step: int,
                    msgs: list[MessageSpec], ep: Endpoint) -> bytes: ...  # GRIB2-Bytes, bereits zugeschnitten
```

Jede Quelle kommt mit einer YAML-Datei (wie im PredictionDB-Entwurf), z. B.:

```yaml
code: gfs_0p25
schedule: "*/10 * * * *"           # alle 10 min auf neue Schritte prüfen
runs: ["00", "06", "12", "18"]
steps: {range: [[0, 120, 1], [123, 384, 3]]}
bbox: {north: 58, south: 44, west: 0, east: 20}
endpoints:
  - {kind: nomads_filter, url: "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25_1hr.pl", rate_per_min: 50}
  - {kind: idx_range,     url: "https://noaa-gfs-bdp-pds.s3.amazonaws.com"}
  - {kind: idx_range,     url: "https://storage.googleapis.com/global-forecast-system"}
messages:
  - {var: TMP,   level: "2 m above ground"}
  - {var: UGRD,  level: "100 m above ground"}
  - {var: VGRD,  level: "100 m above ground"}
  - {var: DSWRF, level: surface}
  # ...
```

### 6.4 Ablauf eines Ingest-Zyklus

1. Scheduler löst aus → `expected_runs()` liefert Läufe der letzten n Stunden.
2. Für jeden nicht vollständigen Lauf: fehlende Nachrichten aus `meta.grib_message` bestimmen.
3. Endpunkt nach Priorität wählen; Verfügbarkeit prüfen (`.idx` vorhanden bzw. HTTP 200 auf HEAD).
4. Download (gebündelt je Schritt), Zuschnitt, SHA-256, Upload nach Bronze, Status `stored`.
5. Lauf vollständig → `meta.nwp_run.status = 'complete'` + `NOTIFY nwp_run_complete`.
6. Fehler → Backoff; nach n Fehlschlägen nächster Endpunkt; Metrik `ingest_failures_total{source,endpoint}`.
7. Nachholmodus (`backfill`) nutzt dieselbe Logik über einen Zeitraum, ausschließlich gegen Archiv-Endpunkte (AWS), mit separatem Rate-Limit.

```mermaid
flowchart TD
    T(["Scheduler-Tick"]) --> R["expected_runs()"]
    R --> L{"Lauf vollständig?"}
    L -- ja --> T
    L -- nein --> M["fehlende Nachrichten<br/>aus meta.grib_message"]
    M --> E["nächster Endpunkt<br/>nach Priorität"]
    E --> A{"verfügbar?<br/>(.idx / HEAD)"}
    A -- nein --> W["später erneut"] --> T
    A -- ja --> D["Download gebündelt je Schritt"]
    D --> OK{"erfolgreich?"}
    OK -- nein --> BO["Backoff, Fehlerzähler"]
    BO --> CB{"Zähler > n?"}
    CB -- ja --> E
    CB -- nein --> D
    OK -- ja --> ST["Zuschnitt, SHA-256,<br/>Upload Bronze, Status stored"]
    ST --> C{"alle Nachrichten<br/>stored?"}
    C -- nein --> M
    C -- ja --> N["nwp_run = complete<br/>NOTIFY nwp_run_complete"]
    N --> T
```

### 6.5 Konzepte aus dem Altsystem

| Altes Konzept | Übernahme |
|---|---|
| `grib_finished.txt` (Liste vollständiger Läufe) | `meta.nwp_run` mit Status `complete`; dieselbe Rolle, aber mit Quelle, Endpunkt und Teilstatus je Nachricht |
| `get_cgi_data.sh?param&ort&lauf` (Punktzeitreihe je Lauf) | API-Endpunkt `GET /forecast/point?model=&param=&lat=&lon=&run=latest` liest aus Silber (Zarr), interpoliert bilinear. Benannte Orte als Tabelle `meta.location`. |
| „Jüngster Lauf, der den Zeitpunkt abdeckt“ (`findMostActualForcast`) | Standardabfrage `run_time = max(run_time) WHERE run_time + Verzug ≤ issue_time`; zugleich der Leakage-Schutz aus 7.4 |
| Inventar-Nummern (195 = T2, 245 = DSWRF) | Ersetzt durch Kurzname + Level (`TMP:2 m above ground`); Nummern sind produktabhängig und haben sich seitdem geändert |
| Assessments (Ähnlichkeit von Tagen über Temperaturmittel und Strahlungssumme) | Analog-Tag-Baseline in Phase 5 (7.3) |
| Testdateien des alten GRIB-Bundles | Fixtures für `gribkit`-Tests (GRIB2 JPEG2000, GRIB1, Einzelnachricht) |

Hinweis zum GRIB-Parsing: Ein eigener GRIB2-Parser in reinem Python lohnt nicht. GRIB2 hat zahlreiche Packungsarten (JPEG2000, CCSDS/AEC, komplexe Packung mit räumlicher Differenzierung), die NOAA, ECMWF und DWD tatsächlich nutzen. `eccodes` (ECMWF, Apache-2.0, Wheels inklusive Bibliothek) deckt das ab. Eigener Code beschränkt sich auf Index-Parsing, Range-Merging und Zuschnitt (`gribkit`).

---

## 7. Machine Learning

### 7.1 Zielgrößen und Horizonte

| Ziel | Auflösung | Horizont | Bemerkung |
|---|---|---|---|
| Netzlast DE (ENTSO-E „Actual Total Load“) | 15 min (Training ggf. stündlich) | 0–16 Tage | Wetter (Temperatur), Kalender, Saison dominieren |
| Day-Ahead-Preis DE-LU | 15 min | D+1 (Kern), D+2 … D+14 (Szenario) | Treiber: Residuallast = Last − Wind − PV, Nachbarländer, Brennstoffpreise |
| Hilfsziele: Wind-, PV- und Laufwasser-Einspeisung DE | 15 min | 0–16 Tage | verbessern Preisprognose deutlich (Residuallast) |

Ehrliche Erwartung: Für D+1 lässt sich ein guter Preis erreichen; jenseits von etwa 5–7 Tagen wird vor allem die Struktur (Tagesgang, Wochenende, wind-/sonnenreiche Phasen) prognostiziert, nicht der genaue Preis. Deshalb Quantilprognosen (z. B. P10/P50/P90) statt Punktwerten.

### 7.2 Features

- **Wetter je Region bzw. Anlagen-Cluster** (aus Silber berechnet, Details in 7.5):
  - Temperatur bevölkerungsgewichtet (Last),
  - Wind-Leistungsindex je Wind-Cluster: Windgeschwindigkeit 100 m → Leistungskurve (nach Nabenhöhe/Anlagentyp), mal installierte Leistung des Clusters,
  - PV-Index je PV-Cluster: Globalstrahlung × Temperaturkorrektur (Ausrichtung/Neigung, soweit im MaStR angegeben), mal installierte Leistung,
  - Bewölkung, Niederschlag, Schnee (Schneebedeckung von PV).
- **Mehrere Modelle nebeneinander** (GFS, ECMWF, ICON) plus Spread zwischen ihnen und GEFS-Streuung als Unsicherheitsmaß; je Modell ein Verfügbarkeits-Flag.
- **Vorlaufzeit** als explizites Feature (Modellgüte nimmt mit dem Vorlauf ab).
- **Kalender:** Stunde, Wochentag, Feiertag (bundesweit/anteilig je Land), Brückentage, Ferienanteil, Zeitumstellung.
- **Markt:** zuletzt bekannte Preise und Lasten (Lags, nur was zum Ausgabezeitpunkt veröffentlicht war), installierte Kapazitäten, optional Brennstoff-/CO₂-Preise.

### 7.3 Modellansatz

1. **Baseline:** saisonal-naive Prognose, Analog-Tag-Verfahren (die Assessment-Idee aus dem Altsystem: ähnlichste historische Tage nach Wetterprognose suchen und deren Ist-Werte gewichten) und LightGBM je Horizont-Block. Das ist der Maßstab, den jedes NN schlagen muss.
2. **Neuronales Netz:** Multi-Horizont-Encoder-Decoder für Zeitreihen mit bekannten Zukunftskovariaten. Kandidaten: TFT (Temporal Fusion Transformer), TiDE, N-HiTS mit exogenen Variablen. Quantile-Loss, gemeinsames Modell für Last, Wind, PV und Preis (Multi-Task) oder zweistufig: Stufe 1 Last/Wind/PV, Stufe 2 Preis mit Residuallast als Eingang. Empfehlung: zweistufig, weil sich Fehler so besser zuordnen lassen.
3. Später optional: räumliches Modell, das direkt auf den Silber-Gittern arbeitet (CNN-Encoder über das Europa-Gitter), falls die Regionen-Aggregation Information verschenkt.

**Umsetzungsstand (10/2026):** Das erste Modell ist die LightGBM-Stufe aus Punkt 1, zweistufig
wie unten, als Dienst `ml-train` (`services/ml_train`, Konfiguration `config/ml/de_lu_v1.yaml`):
Quantile P10/P50/P90 je Stunde für D+1 bis D+7, Ausgabe täglich 08 UTC, Wind und PV als
Kapazitätsfaktor, Preis relativ zum letzten bekannten Liefertag. Ohne Wetter schlägt es bei der
Last bereits die ENTSO-E-Day-Ahead-Prognose (MAE D+1 1,73 gegen 1,98 GW, 2024–10/2026), beim
Preis ab D+2 Vortag und Vorwoche. Die Variante mit GFS-Features wird nach dem Backfill bewertet.
Backtest und Befunde: [`docs/analysen/prognose-de-lu-v1/`](analysen/prognose-de-lu-v1/README.md).
Die zweite Konfiguration `de_lu_v2` ergänzt freie Reihen als Merkmalsgruppen (Nachbarpreise,
ÜNB-Lastprognose, Brennstoffe, Marktwerte, EIA STEO, Grenzkostenindex aus SMARD); `ml-predict`
rechnet beide Linien stündlich nebeneinander
([`docs/analysen/prognose-de-lu-v2/`](analysen/prognose-de-lu-v2/README.md)). Aufbau, technische Daten, Training und Optimierungsstrategien der Modelle im Einzelnen:
[PROGNOSEMODELLE.md](PROGNOSEMODELLE.md).

Zweistufiger Aufbau:

```mermaid
flowchart LR
    subgraph IN["Eingänge zum Ausgabezeitpunkt"]
        W["Wetter-Features je Cluster<br/>GFS, ECMWF, ICON + Spread"]
        K["Kalender<br/>Feiertage, Ferien, Stunde"]
        CAP["installierte Leistung<br/>je Cluster (MaStR)"]
        LAG["veröffentlichte Lags<br/>Preis, Last"]
        NB["Nachbarländer,<br/>optional Brennstoff/CO₂"]
    end

    subgraph S1["Stufe 1"]
        LOAD["Last DE"]
        WIND["Wind-Einspeisung"]
        PV["PV-Einspeisung"]
    end

    RES["Residuallast =<br/>Last − Wind − PV"]

    subgraph S2["Stufe 2"]
        PRICE["Day-Ahead-Preis DE-LU<br/>P10 / P50 / P90"]
    end

    W --> LOAD & WIND & PV
    K --> LOAD
    CAP --> WIND & PV
    LAG --> LOAD
    LOAD & WIND & PV --> RES
    RES --> PRICE
    K --> PRICE
    LAG --> PRICE
    NB --> PRICE
```

### 7.4 Training, Validierung, Betrieb

- **Leakage-Schutz:** Trainingsbeispiele werden ausschließlich aus Läufen gebildet, die zum Ausgabezeitpunkt verfügbar waren (`run_time + Veröffentlichungsverzug ≤ issue_time`), Marktdaten über `published`.

  ```mermaid
  sequenceDiagram
      participant N as NOAA GFS 06Z
      participant D as PredictionDB
      participant M as Börse (SDAC)
      Note over N: run_time 06:00 UTC
      N->>D: Schritte verfügbar ab ca. 09:30 UTC
      Note over D: issue_time 10:00 UTC:<br/>nur Läufe mit run_time + Verzug ≤ issue_time
      D->>D: Prognose für Liefertag D+1
      Note over M: Gebotsschluss 12:00 MEZ/MESZ
      M->>D: Auktionsergebnis ca. 13:00 MEZ/MESZ<br/>(published) → Ziel für das Training
  ```
- **Validierung:** rollierende Zeitreihen-Kreuzvalidierung (Expanding Window), Metriken MAE/RMSE je Horizont, Pinball-Loss, für Preis zusätzlich Rang-/Spread-Güte (wichtig für Lastverschiebung).
- **Training** als portabler Container-Job (`docker compose run ml-train …` oder auf einer gemieteten GPU), auf der VM CPU-basiert machbar für Regionen-Features. Hardware, Laufzeiten und Ablauf in 7.7.
- **Inferenz** (`ml-predict`): reagiert auf `NOTIFY features_ready`, lädt das in MLflow als `production` markierte Modell, schreibt Quantile nach `fcst.prediction`. Umgesetzt ist vorerst eine Datei-Registry in Garage (`s3://ml-artifacts/models`) und eine stündliche Inferenz aller aktiven Versionen (Champion, Challenger, Baseline, Vorgänger) mit Nachholen verpasster Stunden; neue Versionen rechnen die Vergleichszeitpunkte der letzten neun Tage nach (`--fill-new`), damit sie sofort im Dashboard stehen ([PROGNOSEMODELLE.md](PROGNOSEMODELLE.md) Kap. 8).
- **Monitoring:** täglicher Abgleich Prognose ↔ Ist in Grafana; Drift-Alarm bei Fehleranstieg.

### 7.5 Anlagenverteilung aus dem MaStR und Gebietscluster

Ziel: Wetterprognosen dort auswerten, wo die Wind-, PV- und anderen EE-Anlagen tatsächlich stehen, und dem Netz pro Gebiet ein Signal „erwartete Einspeisung“ geben.

```mermaid
flowchart TB
    Z["MaStR-Gesamtexport<br/>quartalsweise"] --> B[("assets-bronze<br/>Original-ZIP")]
    B --> P["Parsen<br/>lxml.iterparse"]
    P --> U[("assets.unit<br/>je Snapshot")]
    U --> GEO["Verorten<br/>Koordinaten oder<br/>Gemeinde-Schwerpunkt"]
    GEO --> CL["Clustern je Technologie<br/>leistungsgewichtetes k-Means"]
    U --> HIST["Bestand je Tag<br/>aus Inbetriebnahme/Stilllegung"]
    CL --> CAPD[("assets.capacity_daily")]
    HIST --> CAPD
    CL --> GW[("Gitter-Gewichte je Cluster<br/>und Wettermodell<br/>assets.grid_weight")]
    SIL[("Silber: Wetter-Zarr<br/>je Lauf")] --> MUL["Gewichte × Prognose"]
    GW --> MUL
    MUL --> F[("feat.nwp_value<br/>Leistungsindex je Cluster")]
    CAPD --> NN["Neuronales Netz"]
    F --> NN
```

**Abruf (quartalsweise):**

- Quelle: MaStR-Gesamtdatenexport (<https://www.marktstammdatenregister.de/MaStR/Datendownload>), ZIP mit XML-Dateien je Einheitentyp (mehrere GB entpackt), Lizenz DL-DE BY 2.0.
- Dienst `mastr-snapshot` (Paket `services/mastr_ingest`, CLI `mastr-ingest`), prüft alle 6 h, ob im laufenden Quartal schon ein Snapshot liegt, und lädt sonst den Export des Tages (manuell auslösbar). Das Original-ZIP landet in Garage `assets-bronze/mastr/YYYY-MM-DD/` und bleibt erhalten, damit Auswertungen reproduzierbar sind.
- Parsing mit eigenem Streaming-XML-Parser (`lxml.iterparse`, UTF-16), weil `open-mastr` alle Tabellen über pandas in eine eigene DB schreibt und unter AGPL steht. Aufzählungen werden über `Katalogwerte.xml` als Text ausgewertet, damit neue Releases mit verschobenen Ids nichts brechen.
- Relevante Felder je Einheit: Energieträger, Bruttoleistung/Nettonennleistung, Inbetriebnahme- und Stilllegungsdatum, Betriebsstatus, Koordinaten bzw. PLZ/Gemeinde, bei Wind Nabenhöhe, Rotordurchmesser, Hersteller/Typ, on-/offshore, bei PV Hauptausrichtung, Neigung, Freifläche/Dach, Einspeiseart (Voll-/Überschusseinspeisung, Eigenverbrauch).

**Verortung:**

- Koordinaten sind im öffentlichen Export nur für größere Anlagen enthalten; kleine Anlagen (insbesondere Dach-PV) nur mit PLZ bzw. Gemeinde. Diese werden auf den Schwerpunkt der Gemeinde (Geometrien: BKG VG250, frei, DL-DE BY 2.0) oder gleichmäßig auf deren Fläche verteilt. Die genaue Schwelle prüfe ich beim ersten Import.
- Offshore-Wind über Koordinaten bzw. Windpark-Zuordnung zu den Nordsee-/Ostsee-Gebieten.

**Zeitliche Rekonstruktion:**

- Aus Inbetriebnahme- und Stilllegungsdatum ergibt sich für jeden Tag der Historie die installierte Leistung je Gebiet. Damit sieht das Training für 2022 den Anlagenbestand von 2022 und nicht den heutigen (sonst wäre der Zubau, vor allem PV, falsch abgebildet).
- Ein Quartals-Snapshot genügt dafür, weil der Export auch stillgelegte Anlagen mit Datum enthält. Nachmeldungen werden durch spätere Snapshots korrigiert; die Differenz zwischen Snapshots wird geloggt.

**Clustering:**

- Getrennt je Technologie (Wind onshore, Wind offshore, PV Freifläche, PV Dach, Biomasse, Wasser), weil Wetterabhängigkeit und Tagesprofil verschieden sind.
- Leistungsgewichtetes k-Means bzw. Mini-Batch-k-Means auf (lat, lon), optional plus Nabenhöhe bzw. Ausrichtung als Zusatzdimension. Startwert z. B. 20–30 Cluster für Wind onshore, 20–30 für PV, 3–5 für Offshore; die Zahl wird über die Prognosegüte gewählt. Alternative mit festen Grenzen: Regelzonen × Bundesländer (stabil, gut erklärbar, aber gröber).
- Cluster werden nur bei signifikanter Änderung neu gebildet (z. B. jährlich), damit sich die Feature-Definitionen für das Netz nicht jedes Quartal verschieben. Zwischen Neubildungen werden neue Anlagen dem nächstgelegenen bestehenden Cluster zugeordnet. Jede Clusterung bekommt eine Version; das Modell merkt sich, mit welcher Version es trainiert wurde.

**Kombination mit der Wetterprognose:**

- Für jedes Cluster eine Gewichtsmatrix „Leistung je Gitterzelle“ pro Wettermodellgitter (GFS 0,25°, ECMWF 0,25°, ICON-EU 0,0625°). `wx-process` multipliziert jede Prognose mit dieser Matrix und schreibt je Cluster, Lauf und Gültigkeitszeit Kennzahlen nach `feat.nwp_value` (Leistungsindex in MW, mittlere Windgeschwindigkeit bzw. Einstrahlung).
- Region-Codes in `meta.region` erhalten dafür die Form `wind_on:v2025Q4:c07`; Gitter-Gewichte liegen in der Tabelle `assets.grid_weight` (je Gitter, Cluster und Zelle Leistung und Anteil; wenige tausend Zeilen), daraus baut `wx-process` die dichte Matrix.
- Umgesetzt in `services/wx_process`: je Cluster gewichtetes Mittel über die Zellen, nichtlineare Größen (Wind in Nabenhöhe nach Potenzgesetz mit Exponent aus 10 m/100 m und der mittleren MaStR-Nabenhöhe, generische Leistungskurve, einfaches PV-Modell aus Globalstrahlung und Temperatur) erst je Zelle. Features `wind_speed_*`, `wind_cf`, `wind_power` (MW mit `assets.capacity_daily`), `ghi`, `pv_cf`, `pv_power`, `cloud_cover`, `snow_fraction`, `precip`, `t2m`; Aggregation `cluster_cap_weighted`, Zustand je Lauf, Clusterung und Feature-Version in `meta.nwp_feature_run`. Details: `services/wx_process/README.md`.

**Ergänzende Tabellen:**

```sql
CREATE TABLE assets.snapshot (
  snapshot_id serial PRIMARY KEY, export_date date UNIQUE, object_key text, n_units int
);
CREATE TABLE assets.unit (                       -- reduzierte, normalisierte Sicht auf den Export
  snapshot_id int, mastr_nr text, technology text, capacity_kw real,
  commissioned date, decommissioned date, lat real, lon real, geo_precision text,  -- 'coord'|'gemeinde'|'plz'
  hub_height_m real, azimuth text, tilt text,
  PRIMARY KEY (snapshot_id, mastr_nr)
);
CREATE TABLE assets.cluster (
  clustering_version text, technology text, cluster_no int, centroid_lat real, centroid_lon real,
  PRIMARY KEY (clustering_version, technology, cluster_no)
);
CREATE TABLE assets.capacity_daily (             -- rekonstruierte installierte Leistung (Hypertable)
  clustering_version text, technology text, cluster_no int, day date, capacity_mw real,
  PRIMARY KEY (clustering_version, technology, cluster_no, day)
);
```

- **Wasserkraft im Systemmodell:** MaStR führt Wasserkraftanlagen mit Art (Laufwasser, Speicher, Pumpspeicher), Leistung und meist Koordinaten. Laufwasser wird nicht per k-Means geclustert, sondern nach **Flussgebieten** (z. B. Inn, Isar, Lech, Iller, Donau, Hochrhein, Main, Mosel, sonstige), weil Anlagen am selben Fluss denselben Abfluss sehen. Je Cluster gehen ein, jeweils mit den Nachlauf- und Gedächtnis-Features aus 7.6: Abfluss an zwei bis drei Leitpegeln (PEGELONLINE bzw. Landesportale, Ist-Werte), GloFAS-Abflussprognose, gebietsgemittelter Niederschlag und Schneeschmelze aus den Wettermodellen. Zielgröße für das Lernen ist die ENTSO-E-Erzeugung „Hydro Run-of-river and poundage“ für DE. Pumpspeicher und Speicher werden nicht aus dem Wetter prognostiziert. Die Füllstände der Speicher in den Nachbarländern (ENTSO-E „Water reservoirs and hydro storage plants“, wöchentlich, vor allem CH, AT, FR, NO) gehen als Preis-Feature ein, weil sie das Angebot an flexibler Wasserkraft bestimmen.

Umgesetzt in Revision `0002_assets` mit einigen Ergänzungen: `assets.clustering` (Version, Snapshot, Gitter), `assets.cluster_cell` (Zelle → Cluster), `assets.grid_weight` und `assets.gemeinde` (optionale Referenzpunkte, z. B. aus VG250). Geclustert wird auf den Zellen des GFS-0,25°-Gitters (summierte Leistung je Zelle), sodass jede Zelle genau einem Cluster gehört. Ohne Koordinaten wird eine Einheit über Gemeinde, PLZ, Kreis und Bundesland verortet (Mittel der Einheiten mit Koordinaten bzw. Referenzpunkt); `geo_precision` hält die Stufe fest. Seit Revision `0016_unit_regelzone` trägt jede Einheit zusätzlich Regelzone und Netzbetreiber ihres Netzanschlusspunkts (über `LokationMaStRNummer` → `Netzanschlusspunkte`), ohne Netzanschlusspunkt die häufigste Regelzone ihrer Gemeinde, PLZ, ihres Kreises, Seegebiets oder Bundeslands (`regelzone_source`); `assets.zone_weight` hält die Leistung je Cluster, Regelzone und Zelle. Wasserkraft wird vorerst wie die übrigen Technologien per k-Means geclustert; die Gruppierung nach Flussgebieten (oben) ersetzt das später. Details: `services/mastr_ingest/README.md`.

**Wirkung im Netz:** Die Cluster-Leistungsindizes sind die wichtigsten Eingänge der Stufe 1 (Wind- und PV-Einspeisung) aus 7.3; deren Summe ergibt mit der Lastprognose die Residuallast für die Preis-Stufe. Die installierte Leistung je Cluster geht zusätzlich als Feature ein, damit das Netz den Zubau lernt.

### 7.6 Prognose für eigene Anlagen (PV, Wind, Wasserkraft) über die API

Ziel: Über die API lässt sich eine einzelne PV-, Windkraft- oder Wasserkraftanlage (vom großen Laufwasserkraftwerk bis zur privaten Kleinanlage) mit ihren Stammdaten anlegen, optional mit rund einem Jahr Messwerten. Die API liefert dann laufend eine Erzeugungsprognose aus den Wetterdaten, die ohnehin geladen werden. Das passt zum Additional Use Grant der Lizenz (Prognose eigener Anlagen an eigenen Standorten, Kap. 12).

Der Ansatz ist zweistufig: Ein **physikalisches Modell** liefert sofort nach dem Anlegen eine Prognose. Sobald Messwerte vorliegen, korrigiert eine **Kalibrierung** dessen systematische Fehler.

```mermaid
flowchart LR
    S["POST /plants<br/>Stammdaten"] --> P[("assets.own_plant")]
    M["PUT /plants/{id}/measurements<br/>Historie, optional"] --> MS[("assets.own_measurement")]
    SIL[("Silber: Wetter-Zarr<br/>je Lauf")] --> X["Punktextraktion<br/>am Standort bzw.<br/>Einzugsgebietsmittel"]
    PG["Pegel und<br/>Abflussprognosen"] --> X
    P --> X
    X --> PHY["Physikalisches Modell<br/>PV: pvlib · Wind: Leistungskurve<br/>Wasser: Abfluss × Fallhöhe"]
    MS --> CAL["Kalibrierung<br/>gegen Hindcasts"]
    PHY --> CAL
    CAL --> COR["Korrekturmodell<br/>je Anlage"]
    R["PUT /plants/{id}/curtailment-rules<br/>Fledermaus, Vogel, Schall"] --> AB["Abschaltregeln<br/>auf Wetter anwenden"]
    PHY --> AB
    AB --> F[("fcst.plant_forecast")]
    COR --> AB
    F --> G["GET /plants/{id}/forecast"]
```

**Endpunkte** (FastAPI, Teil des Dienstes `api`, Auth per API-Schlüssel; jede Anlage gehört einem Schlüssel bzw. Nutzer):

| Methode und Pfad | Zweck |
|---|---|
| `POST /plants` | Anlage anlegen (`type`: `pv`, `wind` oder `hydro`, Stammdaten siehe unten); Antwort mit `plant_id` |
| `GET/PATCH/DELETE /plants/{id}` | Stammdaten lesen, ändern, Anlage samt Messwerten löschen |
| `PUT /plants/{id}/measurements` | Messwerte hochladen (CSV oder JSON: Zeitstempel UTC, Leistung in kW oder Energie je Intervall in kWh, Intervall 15 min oder 1 h). Typischer Umfang ist ein Jahr, mehr ist besser. Mehrfaches Hochladen ergänzt bzw. überschreibt Zeiträume. |
| `POST /plants/{id}/calibrate` | Kalibrierung anstoßen (asynchron, Status über `GET /plants/{id}/calibration`). Läuft auch automatisch nach einem Upload. |
| `GET /plants/{id}/forecast` | Prognose; Parameter `model` (Standard `best`, sonst `gfs`, `ecmwf`, `icon_eu` …), `run` (`latest` oder Zeitpunkt), `horizon_h` (bis 384 bei GFS), `resolution` (15 min / 1 h), `quantiles` |
| `PUT /plants/{id}/curtailment-rules` | Abschaltregeln eintragen (Artenschutz, Schall, Abregelung), siehe unten |
| `GET /plants/{id}/backtest` | Güte der Prognose gegen die hochgeladenen Messwerte (MAE, RMSE, Bias je Vorlauftag) |

**Stammdaten PV:**

- Standort: `lat`, `lon`, optional Höhe über NN.
- Eine oder mehrere **Teilflächen** (Ost/West-Dächer, Fassaden), je Teilfläche `peak_power_kwp`, `azimuth_deg` (Konvention 0 = Nord, 90 = Ost, 180 = Süd, 270 = West) und `tilt_deg` (0 = waagerecht).
- Optional: Wechselrichter-AC-Leistung (Clipping), Einspeisebegrenzung (z. B. 60 % oder 70 %), Modultechnik bzw. Temperaturkoeffizient, Montageart (Aufdach, Freifläche, gebäudeintegriert; bestimmt die Zelltemperatur), Horizont- bzw. Verschattungsprofil, Systemverluste, Inbetriebnahmedatum (Degradation).

**Stammdaten Wind:**

- Standort: `lat`, `lon`, optional Geländehöhe.
- `hub_height_m`, `rotor_diameter_m`, `rated_power_kw`.
- Leistungskurve: entweder Hersteller und Typ (Nachschlagen in einer Turbinen-Bibliothek, z. B. der oedb-Turbinendaten, die `windpowerlib` nutzt) oder eine eigene Kurve als Tabelle (Windgeschwindigkeit → Leistung) bzw. `cp`-Kurve.
- Optional: Ein- und Abschaltgeschwindigkeit, Anzahl gleicher Anlagen (Park), Verfügbarkeit, Rauigkeitslänge bzw. Geländetyp, Abschaltauflagen (Fledermäuse, Vögel, Schall, Schattenwurf) als Regeln, siehe unten.

**Stammdaten Wasserkraft:**

- Standort: `lat`, `lon` der Wasserfassung bzw. des Krafthauses, Gewässername.
- `head_m` (Fallhöhe, brutto bzw. netto), `design_flow_m3s` (Ausbaudurchfluss), `rated_power_kw`, Anzahl Maschinen.
- Turbinentyp (Kaplan, Francis, Pelton, Durchström-/Ossberger, Wasserrad, Wasserkraftschnecke) und optional eine eigene Wirkungsgradkurve (Durchfluss → Wirkungsgrad). Fehlt sie, gilt eine typische Kurve je Turbinentyp.
- Mindestdurchfluss je Maschine (unterhalb steht die Turbine), Restwasser- bzw. Mindestwasserauflage (konstant oder je Monat, z. B. nach Bescheid), Abschaltdurchfluss bei Hochwasser, optional die Fallhöhenabnahme bei hohem Abfluss (steigendes Unterwasser).
- Hydrologischer Bezug, mindestens eine der Angaben:
  - zugehöriger **Pegel** (PEGELONLINE-ID oder Messstellennummer eines Landesportals) samt Flächenverhältnis Anlage zu Pegel,
  - **Einzugsgebiet** als Polygon (GeoJSON) oder als Fläche in km². Ohne Polygon wird es aus einem freien Höhenmodell (Copernicus DEM 30 m) ab dem Standort abgeleitet, z. B. mit `pysheds`.
- Bei Speicher- und Pumpspeicheranlagen zusätzlich Speichergröße. Deren Einsatz folgt aber vor allem dem Strompreis und lässt sich aus dem Wetter allein nicht prognostizieren (siehe unten).

**Abschaltauflagen für Fledermäuse und Vögel (Wind):**

Viele Genehmigungen schreiben Abschaltungen zum Artenschutz vor. Diese Zeiten fehlen in jeder reinen Wetterprognose und müssen deshalb als Regeln je Anlage hinterlegt werden. Der Anwender trägt sie über `PUT /plants/{id}/curtailment-rules` ein, typischerweise aus dem Genehmigungsbescheid.

- **Fledermäuse:** Die Auflagen sind meist rein wetter- und zeitabhängig und lassen sich deshalb vorhersagen. Ein typisches Beispiel: 1. April bis 31. Oktober, von einer Stunde vor Sonnenuntergang bis Sonnenaufgang, wenn die Windgeschwindigkeit auf Nabenhöhe unter 6 m/s liegt, die Temperatur über 10 °C und es nicht regnet. Die Werte stehen im Bescheid und ändern sich oft nach dem Gondelmonitoring der ersten Betriebsjahre (z. B. ProBat). Deshalb sind die Regeln versioniert und haben ein Gültigkeitsdatum.
- **Vögel:** Hier gibt es zwei Arten. Feste Zeitfenster wie Brutzeit oder Zugzeit, tagsüber zwischen Sonnenauf- und -untergang, ggf. mit Wetterbedingung, werden wie bei Fledermäusen gerechnet. Ereignisgesteuerte Abschaltungen, z. B. bei Mahd, Ernte oder Bodenbearbeitung im Umkreis (häufig beim Rotmilan, am Tag des Ereignisses und an den Folgetagen), sind nicht aus dem Wetter ableitbar. Der Anwender trägt sie als konkrete Termine ein, sobald sie bekannt sind. Ohne Termin geht für die Erntemonate optional ein erwarteter Verfügbarkeitsabschlag in die Quantile ein. Kamerabasierte Antikollisionssysteme werden ebenfalls als pauschaler Abschlag je Monat und Tageszeit modelliert.
- **Regelformat** (JSON, per Pydantic validiert; mehrere Regeln je Anlage, verknüpft mit ODER):

```json
{
  "name": "Fledermaus Bescheid 2024",
  "valid_from": "2024-04-01", "valid_to": null,
  "months": [4, 5, 6, 7, 8, 9, 10],
  "time_window": {"start": {"ref": "sunset", "offset_min": -60}, "end": {"ref": "sunrise", "offset_min": 0}},
  "conditions": {"wind_hub_ms": {"lt": 6.0}, "t2m_c": {"gt": 10.0}, "precip_mm_h": {"lt": 0.2}},
  "action": {"type": "stop"}
}
```

`ref` kann `sunrise`, `sunset`, `civil_dawn`, `civil_dusk` oder eine feste Uhrzeit (Ortszeit) sein; Sonnenzeiten rechnet `pvlib` aus dem Standort. `action` ist `stop` oder `limit` mit `max_kw` (z. B. reduzierter Betrieb für Schallschutz). Ereignisse wie Mahd werden als `{"type": "event", "from": …, "to": …}` eingetragen.
- **Anwendung in der Prognose:** Die Regeln werden auf die prognostizierten Wetterwerte am Standort angewandt. Weil die Bedingungen an Schwellen hängen (Wind knapp unter 6 m/s), wird je Quantil bzw. Ensemble-Mitglied geprüft, nicht nur auf dem Median. Die Prognose liefert dadurch Leistung **mit** und **ohne** Abschaltung sowie die Wahrscheinlichkeit, dass eine Abschaltung greift. Das Ergebnis ist auch für die Ertragsplanung nützlich, weil die Abschaltverluste je Monat sichtbar werden.
- **Kalibrierung:** In den Messwerten markieren die Regeln die Zeiträume, in denen die Anlage absichtlich stand. Diese gehen nicht in die Leistungskurve ein. Umgekehrt meldet der Backtest auffällige Stillstände, die zu keiner Regel passen (fehlende Regel, Störung oder Abregelung durch den Netzbetreiber).
- Für PV und Wasserkraft gibt es dieselbe Regel-Schnittstelle, bei Wasserkraft z. B. für monatliche Restwasserauflagen, Fischschutz- bzw. Wanderzeiten oder Revisionen. Bei PV dient sie für Abregelungen, etwa feste Einspeisegrenzen oder Steuerbefehle nach § 14a bzw. Redispatch, soweit sie bekannt sind.

**Physikalisches Modell:**

- **Wetter am Standort:** `wx-process` bzw. `ml-predict` liest je Lauf die benötigten Variablen aus Silber und interpoliert bilinear auf den Standort. Das ist eine kleine Zeitreihe je Anlage und Lauf und kostet kaum Rechenzeit. Standorte außerhalb des Ausschnitts (4.3) werden beim Anlegen abgelehnt, bis der Ausschnitt erweitert ist.
- **PV** (Bibliothek `pvlib`): Globalstrahlung (`DSWRF` bzw. `ssrd`) zerlegen in Direkt- und Diffusanteil (Erbs bzw. DISC, bei ICON direkt `aswdir_s`/`aswdifd_s`), Transposition auf jede Teilfläche (Perez), Einfallswinkelverluste, Zelltemperatur aus 2-m-Temperatur, 10-m-Wind und Montageart, dann DC-Leistung, Wechselrichter, Clipping und Einspeisebegrenzung. Wichtig: GFS liefert `DSWRF` als Mittel über wechselnde Intervalle (Rücksetzen alle 6 h); `wx-process` wandelt das in echte Stundenmittel um, bevor es nach Silber geht. Für 15 min wird mit dem Sonnenstand interpoliert, nicht linear.
- **Wind** (Bibliothek `windpowerlib`): Windgeschwindigkeit auf Nabenhöhe aus 10 m und 100 m (bei GFS zusätzlich 80 m) über das gemessene Höhenprofil (Potenzgesetz mit Exponent aus den beiden Niveaus, ersatzweise logarithmisches Profil mit Rauigkeitslänge), Luftdichtekorrektur aus Temperatur und Bodendruck (`PRES:surface` kommt dafür in die Variablenliste 4.3), dann Leistungskurve. Bei Parks ein einfacher Wake-Abschlag als Parameter.
- **Wasserkraft:** Die Leistung hängt am Abfluss, nicht direkt am Wetter. Gerechnet wird `P = ρ · g · Q_T · H(Q) · η(Q_T)` mit `Q_T = min(Q − Q_Rest, Q_Ausbau)`. Unter dem Mindestdurchfluss ist `P = 0`, über dem Abschaltdurchfluss ebenfalls. Der Abfluss `Q` kommt je nach verfügbaren Daten aus einer von drei Quellen, in dieser Rangfolge:
  1. **Abflussprognose eines Dienstes** für den Pegel, soweit kostenfrei verfügbar: Hochwasser- und Abflussvorhersagen der Landesportale sowie GloFAS (Copernicus, global, bis 30 Tage, kostenlos mit Registrierung). EFAS ist für Europa genauer, Echtzeitprognosen gibt es dort aber nur für Partnerbehörden. Für kleine Bäche gibt es meist keine Prognose.
  2. **Eigenes Niederschlag-Abfluss-Modell** je Einzugsgebiet: gebietsgemittelter Niederschlag und Temperatur aus den Wettermodellen, Schneespeicher mit Grad-Tag-Verfahren (Schneeschmelze, wichtig im Alpenvorland), Boden- und Grundwasserspeicher. Ein konzeptionelles Modell wie GR4J oder HBV (eigene kleine Implementierung oder ein vorhandenes Python-Paket, Auswahl im Spike) wird an der Pegel- oder Erzeugungshistorie kalibriert.
  3. **Persistenz**: Ohne Pegel und ohne Historie wird der letzte bekannte Wert fortgeschrieben, mit Rezessionskurve (Trockenwetterabfall). Mit Niederschlag wird angehoben.
  
  Weil Abfluss träge ist, ist der jüngste Mess- oder Erzeugungswert für die ersten Tage der beste Prädiktor. Laufende Messwerte (Push oder Pegel) verbessern die Wasserkraftprognose daher stärker als bei PV und Wind. Die 384 h von GFS liefern vor allem den Trend (Regenphasen, Schneeschmelze).
  **Nachlauf und Gedächtnis:** Bei Wasserkraft wirkt Wetter oft erst mit erheblicher Verzögerung auf den Ertrag. Die Prognose muss deshalb den Zustand des Einzugsgebiets aus der Vergangenheit mitführen und darf nicht erst beim Prognosestart beginnen.

  | Prozess | typischer Nachlauf | Folge für die Prognose |
  |---|---|---|
  | Oberflächenabfluss im kleinen, steilen Gebiet | Stunden | Niederschlag der nächsten Stunden bis Tage zählt |
  | Hochwasserwelle in mittleren Flüssen | 1–5 Tage | Regen von gestern bestimmt den Ertrag von morgen |
  | Zwischenabfluss, Bodenspeicher | Tage bis Wochen | Bodenfeuchte und kumulierter Niederschlag als Zustand |
  | Grundwasser, Basisabfluss | Wochen bis Monate | bestimmt den Ertrag in Trockenphasen, langsame Rezession |
  | Schneedecke | Wochen bis Monate | Winterniederschlag wird erst bei Schmelze (Frühjahr, im Hochgebirge Sommer) wirksam |

  - **Laufzeit je Einzugsgebiet:** Startwert aus Fläche und Gefälle (Konzentrationszeit), dann aus der Historie geschätzt (Kreuzkorrelation Niederschlag → Abfluss bzw. Erzeugung). Bei Flussgebiets-Clustern (7.5) kommt die Fließzeit zwischen Oberlieger-Pegel und Anlage hinzu; dort ist der Pegel weiter oben selbst der beste Prädiktor.
  - **Zustand mitführen:** Das Niederschlag-Abfluss-Modell läuft durchgehend auf beobachteten Werten (DWD RADOLAN-Radarniederschlag bzw. die Analysen der Wettermodelle, Temperatur aus DWD CDC). Bei jedem Lauf startet die Prognose aus diesem Zustand: Schneespeicher, Bodenfeuchte, Grundwasser. Vor dem ersten Einsatz braucht das Modell eine Einlaufzeit von mindestens einem Jahr Wetterhistorie, besser mehreren. Das gilt auch dann, wenn keine Erzeugungshistorie vorliegt.
  - **Gedächtnis-Features** für das Korrekturmodell und das Systemmodell: kumulierter Niederschlag über 1, 3, 7, 14, 30 und 90 Tage (gebietsgemittelt), Schneewasseräquivalent (`WEASD` bzw. `sd`) und positive Gradtage seit Winterbeginn, Bodenfeuchte aus den Modellen (GFS `SOILW`, ECMWF `swvl1`–`swvl4`), aktueller Abfluss bzw. Erzeugung samt Trend und Rezessionskonstante.
  - **Reichweite:** Wegen des Nachlaufs steckt ein großer Teil der Erträge der nächsten Tage schon im System. Die Prognose ist dort auch bei unsicherem Wetter gut. Die 384 h von GFS decken den Niederschlag der nächsten 16 Tage ab, der sich teils erst danach auswirkt; Prognosen über drei bis vier Wochen sind deshalb in Teilen möglich. Darüber hinaus bleiben Schneevorrat gegen Klimatologie und optional Saisonprognosen (ECMWF SEAS5 über den Copernicus CDS, kostenlos mit Registrierung). Die Ausgabe kennzeichnet ab dem Ende der Wetterprognose, dass klimatologisch weitergerechnet wird.
  - **Historie:** Mit Erzeugungsdaten über ein Jahr lassen sich Laufzeit und Speicherparameter schätzen. Zwei bis drei Jahre sind deutlich besser, weil Schneejahre und Trockenjahre stark schwanken.
- **Unsicherheit:** Quantile aus der Streuung der Modelle (GFS, ECMWF, ICON) und den GEFS- bzw. ECMWF-Ensemble-Mitgliedern, solange keine Kalibrierung vorliegt.

**Kalibrierung mit historischen Daten:**

- **Qualitätsprüfung:** Lücken, Nullwerte bei Strahlung bzw. Wind (Ausfall, Abregelung, Schnee auf den Modulen), Zeitzonen- und Sommerzeitfehler (Verschiebung der Mittagsspitze), Einheitenfehler (kW gegen kWh). Verdächtige Zeiträume werden markiert und nicht zum Lernen verwendet.
- **Hindcast statt Ist-Wetter:** Die Messwerte werden mit den archivierten Prognosen verglichen, die damals tatsächlich vorlagen (gleiche Regel wie in 7.4). So lernt die Korrektur auch die typischen Fehler des Wettermodells. Für Zeiträume vor dem eigenen Archiv dienen die Backfill-Quellen aus 3.1 (GFS-Archiv auf AWS, Open-Meteo Historical Forecast für Prototypen).
- **Stufe 1, Parameter nachschätzen:** bei PV effektive Leistung, Ausrichtung und Neigung, Verlustfaktor und Verschattung je Sonnenstand (Azimut × Höhe), bei Wind eine empirische Leistungskurve aus gebinnten Messwerten sowie eine Korrektur je Windrichtung (Gelände, Nachbaranlagen). Bei Wasserkraft werden Flächenverhältnis zum Pegel, Wirkungsgradkurve und Restwasser aus der Beziehung Abfluss → Leistung geschätzt und die Parameter des Niederschlag-Abfluss-Modells an der Erzeugungshistorie kalibriert. Ein Jahr deckt dabei gerade einen Jahresgang ab; zwei bis drei Jahre sind deutlich besser, weil Trockenjahre und Schneejahre stark schwanken.
- **Stufe 2, Korrekturmodell:** Ab etwa einem Jahr Messwerten ein kleines LightGBM-Quantilmodell je Anlage auf den Rest (Messung − Physik). Eingänge sind Vorlauf, Tageszeit, Sonnenstand bzw. Windrichtung, Wettervariablen und Modell-Spread. Mit weniger Daten bleibt es bei Stufe 1. Jede Kalibrierung wird versioniert in MLflow abgelegt. Die API meldet, auf welcher Kalibrierung eine Prognose beruht.
- Wenn regelmäßig neue Messwerte kommen (z. B. per Push aus dem Wechselrichter oder Home Assistant), läuft die Kalibrierung monatlich neu.

**Betrieb:** Nach jedem vollständigen Lauf (`NOTIFY nwp_run_complete`) berechnet `ml-predict` die Prognosen aller registrierten Anlagen und schreibt sie nach `fcst.plant_forecast`. Die API liest daraus und rechnet nur für neu angelegte Anlagen sofort auf Anfrage. Bei einigen hundert Anlagen bleibt das im Sekundenbereich je Lauf.

**Ergänzende Tabellen:**

```sql
CREATE TABLE assets.own_plant (
  plant_id uuid PRIMARY KEY, owner_id text NOT NULL, type text NOT NULL,   -- 'pv'|'wind'|'hydro'
  name text, lat real NOT NULL, lon real NOT NULL, elevation_m real,
  params jsonb NOT NULL,          -- Teilflächen bzw. Turbine, validiert per Pydantic-Schema je Typ
  calibration_version text, created_at timestamptz DEFAULT now()
);
CREATE TABLE assets.own_measurement (             -- Hypertable
  plant_id uuid, ts timestamptz, power_kw real, flag smallint DEFAULT 0,   -- flag: 0 ok, >0 ausgeschlossen
  PRIMARY KEY (plant_id, ts)
);
CREATE TABLE assets.own_curtailment_rule (
  plant_id uuid, rule_id serial, valid_from date, valid_to date, rule jsonb NOT NULL,
  PRIMARY KEY (plant_id, rule_id)
);
CREATE TABLE fcst.plant_forecast (                -- Hypertable, komprimiert
  plant_id uuid, model text, run_time timestamptz, valid_time timestamptz,
  calibration_version text, p10_kw real, p50_kw real, p90_kw real,
  p50_uncurtailed_kw real, curtailment_prob real,  -- ohne Abschaltregeln; Wahrscheinlichkeit einer Abschaltung
  PRIMARY KEY (plant_id, model, run_time, valid_time)
);
```

**Datenschutz:** Standort und Erzeugungsdaten einer privaten Anlage sind personenbezogene Daten. Sie bleiben in der eigenen Datenbank, werden nicht mit dem MaStR-Bestand verknüpft und mit `DELETE /plants/{id}` vollständig entfernt.

**Bezug zum restlichen System:** Die Anlagenprognose braucht nur Silber (Phase 4) und kein trainiertes Netz. Sie kann deshalb früh als erster sichtbarer Nutzen kommen (Phase 4b in Kap. 11). Umgekehrt dient eine gut kalibrierte eigene Anlage als Referenz, um die PV- und Wind-Indizes der Cluster aus 7.5 zu prüfen.

### 7.7 Trainings-Hardware und Training als portabler Job

**Datenmenge:** Das Training ist klein. Zehn Jahre Stundenwerte sind rund 90 000 Zeitpunkte. Dazu kommen einige hundert bis wenige tausend Features (Wetter je Cluster, installierte Leistung, Kalender, Markt-Lags). Die Last steckt im Datenumfang und in der Zahl der Trainingsläufe, nicht in der Modellgröße. Ein Rechenzentrum ist nicht nötig.

| Aufgabe | Hardware | Größenordnung Laufzeit |
|---|---|---|
| LightGBM-Baseline (7.3) | CPU der VM | Minuten |
| Kleine Sequenzmodelle (LSTM, TFT, N-HiTS, TiDE) über 384 h | CPU oder GPU mit 12–16 GB (z. B. RTX 3060 12 GB, RTX 4060 Ti 16 GB) | CPU: Stunden; GPU: etwa 10–60 min je Lauf |
| Hyperparameter-Suche (viele Läufe, z. B. Optuna) | GPU lohnt sich hier am meisten, notfalls nachts auf CPU | Stunden |
| Kalibrierung eigener Anlagen (7.6) | CPU | Sekunden bis Minuten je Anlage |
| Inferenz je Lauf (`ml-predict`) | CPU | Sekunden |
| Gitter-Modelle (Phase 6+, optional) | GPU zwingend | Stunden |

**Was „annehmbar“ heißt:** Ein neuer GFS-Lauf kommt alle 6 h, die Inferenz liegt mit Sekunden weit darunter. In der Entwicklung sollte ein Trainingslauf möglichst unter 1 h bleiben, damit Experimente zügig gehen. Im Betrieb darf ein Neutraining auch deutlich länger dauern (siehe unten).

**Nachtrainieren statt Neutraining:** Ein bestehendes Modell muss nicht jedes Mal von Grund auf neu trainiert werden. Es wird regulär nachgeführt, und nur bei bestimmten Anlässen wird neu trainiert.

| Verfahren | Wann | Aufwand | Risiko |
|---|---|---|---|
| **Fine-Tuning (Warm-Start)** des Produktionsmodells mit den Daten seit dem letzten Training | Standard, wöchentlich | wenige Epochen, Minuten auch auf CPU | Drift Richtung jüngste Wetterlagen |
| **Laufende Nachführung** (Online-Learning, kleine Gradientenschritte nach jedem neuen Ist-Wert) | optional, z. B. täglich für Bias-Korrektur | Sekunden | Instabilität, katastrophales Vergessen seltener Lagen |
| **Neutraining** von Grund auf | bei Strukturänderungen (siehe unten), sonst etwa jährlich als Referenz | Stunden, ggf. Miet-GPU | gering, aber teuer |

- **Gegen Vergessen:** Beim Fine-Tuning werden die neuen Daten mit einer Stichprobe der gesamten Historie gemischt (Replay), gewichtet nach Saison, damit seltene Lagen wie Dunkelflaute, Hitzewelle und Sturm erhalten bleiben. Die Lernrate ist klein, und je Lauf gibt es nur wenige Epochen. Optional werden die unteren Schichten eingefroren.
- **Laufende Nachführung** bevorzugt als leichte Schicht statt im ganzen Netz: ein kleiner Korrekturterm (Bias je Horizont und Tageszeit) oder ein Online-Lerner auf dem Restfehler, der täglich nachgeführt wird. Das Netz selbst bleibt dabei stabil und reproduzierbar. LightGBM lässt sich ebenfalls fortsetzen (`init_model`), bekommt aber in der Praxis meist ein günstiges Neutraining.
- **Neutraining ist nötig,** wenn sich die Eingänge ändern: neue Clusterversion aus dem MaStR (7.5), neue oder entfallene Features, eine neue Wetterquelle, geänderte Zielauflösung (z. B. 15 min) oder eine geänderte Architektur. Ein Neutraining ist auch nötig, wenn der Fehler trotz Fine-Tuning dauerhaft steigt, etwa bei einem Strukturbruch im Markt.
- **Champion/Challenger:** Jedes nachtrainierte oder neue Modell (Challenger) läuft zunächst im Schatten mit und wird auf demselben Validierungszeitraum gegen das Produktionsmodell (Champion) verglichen. Verglichen werden Pinball-Loss je Horizont und getrennt nach Lagen. Nur wenn der Challenger insgesamt besser ist und in keiner wichtigen Lage deutlich schlechter, wird er in MLflow zu `production`. Der Champion bleibt als Rückfall registriert.
- **Mehrere Modelle parallel:** `ml-predict` inferiert nicht nur den Champion, sondern eine feste Gruppe aktiver Versionen nebeneinander, typischerweise den Champion, ein bis zwei Challenger, den Vorgänger-Champion und die LightGBM-Baseline. Die Prognosen aller Versionen landen in `fcst.prediction` (Schlüssel enthält `model_version`). Der Mehraufwand ist gering, weil die Inferenz nur Sekunden dauert.
  - **Abweichung als Signal:** Je Ausgabezeitpunkt und Horizont wird die Streuung zwischen den Versionen berechnet (Spannweite bzw. Standardabweichung der P50-Werte) und in `fcst.model_spread` gespeichert. Eine große Abweichung zeigt eine Lage, die die Modelle unterschiedlich gelernt haben, oder ein Problem mit einem Modell oder seinen Eingängen. Sie geht als zusätzliches Unsicherheitsmaß in die API-Antwort ein und löst in Grafana einen Alarm aus, wenn sie über einer Schwelle liegt (z. B. mehr als das Doppelte des üblichen Spreads für diesen Horizont).
  - **Auswertung:** Mit dem Ist-Wert zeigt die Tabelle für jede Version, wie gut sie in welcher Lage war. Das ist die Grundlage für den Champion/Challenger-Vergleich und für eine spätere gewichtete Kombination der Versionen (Ensemble), falls die besser abschneidet als der Champion allein.
  - **Aufbewahrung:** Aktiv inferiert werden höchstens etwa fünf Versionen. Alle Versionen bleiben in der MLflow-Registry, die Gewichte ausgedienter Versionen in `ml-artifacts` bleiben mindestens ein Jahr erhalten bzw. dauerhaft für jeden früheren Champion. Ihre gespeicherten Prognosen bleiben dauerhaft in der Datenbank, komprimiert über TimescaleDB, damit sich Vergleiche jederzeit nachvollziehen lassen.

```sql
CREATE TABLE fcst.model_spread (                  -- Hypertable
  target text, issue_time timestamptz, valid_time timestamptz,
  n_models smallint, p50_min real, p50_max real, p50_std real,
  PRIMARY KEY (target, issue_time, valid_time)
);
```

Für den **Produktivbetrieb** ist die Trainingszeit deshalb unkritisch: Das bisherige Modell bleibt brauchbar und verliert nur langsam an Genauigkeit, bis ein Nachfolger fertig und geprüft ist. Kurze Laufzeiten zählen vor allem in der **Entwicklung**, damit sich Ideen schnell testen lassen. Dafür sind die lokale GPU bzw. Miet-GPUs gedacht, und dafür gibt es einen kleinen Entwicklungs-Datensatz (z. B. ein Jahr, reduzierte Cluster), der in Minuten durchläuft.

**Stufen:**

1. **Start auf CPU:** LightGBM und erste Sequenzmodelle laufen auf der VM (8.1). Für Trainingsläufe kann die VM vorübergehend mehr vCPUs bekommen.
2. **GPU lokal:** eine durchgereichte Consumer-GPU an die VM (PCIe-Passthrough auf PVE) oder ein separater Trainingsrechner im LAN.
3. **GPU stundenweise mieten:** für Hyperparameter-Suchen oder wenn lokal keine GPU da ist. Eine RTX 3090/4090 reicht für die Modellgrößen hier deutlich. Grobe Preise (Stand Okt. 2026, schwanken stark, vor Nutzung prüfen):

   | Anbieter | Art | Größenordnung RTX 4090 bzw. vergleichbar |
   |---|---|---|
   | Vast.ai | Marktplatz mit privaten Anbietern | ca. 0,20–0,50 $/h; Zuverlässigkeit und Datenschutz je Anbieter verschieden |
   | RunPod | Marktplatz und eigene Rechenzentren, fertige PyTorch-Images | ca. 0,35–0,70 $/h |
   | Lambda, Paperspace, Jarvislabs | klassische Cloud-GPUs zum Festpreis | eher ab 0,50 $/h |
   | Hetzner GPU-Server | Rechenzentrum in Deutschland, monatliche Abrechnung | lohnt erst bei Dauernutzung |
   | Google Colab, Kaggle | kostenlos bzw. günstig | nur für Experimente, Zeitlimits, nicht automatisierbar |

   Eine Hyperparameter-Suche über einige Stunden kostet damit einige Euro.

**Training als portabler Container-Job:** Damit es egal ist, wo trainiert wird, ist `ml-train` ein in sich geschlossenes Image (CPU- und CUDA-Variante aus demselben Dockerfile) mit klarer Ein- und Ausgabe:

```mermaid
flowchart LR
    D["ml-train dispatch<br/>auf der VM"] --> SN["Trainings-Snapshot<br/>Features aus Gold als Parquet<br/>+ Zielgrößen + Konfiguration"]
    SN --> EX[("Garage-Bucket<br/>ml-exchange")]
    EX -- "zeitlich begrenzte<br/>presigned URLs" --> J["ml-train-Container<br/>CPU, lokale GPU<br/>oder Miet-GPU"]
    J --> ART["Modell + Metriken<br/>+ Snapshot-Hash"]
    ART --> EX
    EX --> REG["MLflow-Registry<br/>ml-artifacts"]
    REG --> PRED["ml-predict<br/>nach Freigabe"]
```

- **Eingabe:** Der Dispatcher (Unterbefehl `ml-train dispatch` im selben Image) auf der VM exportiert einen versionierten Trainings-Snapshot (Features und Zielgrößen aus Gold als Parquet, Clusterversion, Trainingskonfiguration) in den Bucket `ml-exchange`. Der Snapshot ist mit einem Hash versehen, damit sich jedes Modell genau einem Datenstand zuordnen lässt.
- **Ausführung:** lokal mit `docker compose run ml-train`, auf einem Rechner im LAN oder auf einer gemieteten GPU-Instanz (Image aus der Registry ziehen, ein Befehl mit Snapshot-URL). Der Job braucht keine Verbindung zu Postgres und keinen Zugang ins Heimnetz.
- **Zugang von außen:** Der Job bekommt nur zeitlich begrenzte presigned URLs (Lesen für den Snapshot, Schreiben für das Ergebnis), erreichbar über einen Reverse Proxy mit TLS, der nur diesen Bucket freigibt. Alternativ läuft die Verbindung über ein VPN (WireGuard bzw. Tailscale). Garage-Schlüssel mit Vollzugriff verlassen die VM nicht.
- **Ausgabe:** Modell, Metriken und Snapshot-Hash zurück nach `ml-exchange`. Der Dispatcher registriert das Ergebnis in MLflow (`ml-artifacts`). Ein Modell geht erst nach dem Vergleich mit dem aktuellen Produktionsmodell auf dem Validierungszeitraum in `production`.
- **Datenschutz:** Trainings-Snapshots für Miet-GPUs enthalten nur öffentliche Daten (Wetter, Markt, MaStR). Messwerte eigener Anlagen (7.6) werden nur lokal verarbeitet. Bei Marktplatz-Anbietern mit privaten Hosts ist das wichtig.
- **Nachtrainieren im Job:** Für ein Fine-Tuning enthält der Snapshot zusätzlich die Gewichte des Champions und die Replay-Stichprobe. Der Job gibt einen Challenger zurück, der dann den Vergleich oben durchläuft.
- **Kostenbremse:** Der Dispatcher setzt eine maximale Laufzeit je Job. Die Miet-Instanz wird nach dem Upload des Ergebnisses beendet.

### 7.8 Geplant: Wärme, Kälte und Wärmepumpen

Diese Features sind vorgemerkt. Die Architektur wird später ausgearbeitet. Schon jetzt werden aber die nötigen Wetterdaten gesammelt (siehe unten), damit beim Start eine Historie vorliegt.

| Feature | Inhalt | Horizonte |
|---|---|---|
| **Wärme- und Kältelast, Wärme- und Kältebedarf** | Prognose für ein einzelnes Gebäude sowie für ein Nah- oder Fernwärmenetz bzw. Kältenetz (Netzlast inkl. Verluste) | Folgetag, kommende 24 h und 96 h |
| **Strombedarf von Wärmepumpen** | elektrische Leistung einer Wärmepumpe aus Wärmebedarf und Leistungszahl (COP), die von Quellen- und Vorlauftemperatur abhängt | wie oben |
| **Betriebsoptimierung eines Wärmepumpen-Heizsystems** | Fahrplan für Wärmepumpe und Wärmespeicher: wann laden, wann aus dem Speicher heizen. Eingänge sind Strompreise (Day-Ahead bzw. dynamischer Tarif), Speichergröße und -füllstand, PV-Prognose der eigenen Anlage (7.6), COP aus der Wetterprognose und Komfortgrenzen. | rollierend, z. B. 24–96 h |

**Benötigte Wetterdaten** (alle im GFS-Umfang 4.3 enthalten und seit 2026-10-21 in Bronze gesammelt):

| Größe | Zweck | GFS-Feld |
|---|---|---|
| Lufttemperatur 2 m | Heiz- und Kühlgradstunden, Transmissionsverluste, COP von Luft-Wärmepumpen | `TMP:2 m above ground` |
| Taupunkt, relative Feuchte | latente Kühllast, Enthalpie, Abtauverluste von Luft-Wärmepumpen (vor allem bei 0–7 °C und hoher Feuchte) | `DPT`, `RH:2 m above ground` |
| Wind 10 m | Lüftungs- und Infiltrationsverluste, Wärmeübergang an der Fassade | `UGRD/VGRD:10 m above ground` |
| Globalstrahlung | solare Gewinne (Heizen) bzw. solare Last (Kühlen), PV-Prognose | `DSWRF:surface` |
| Langwellige Gegenstrahlung | Abstrahlung von Dach und Fassade, Nachtauskühlung | `DLWRF:surface` |
| Bewölkung, Sonnenscheindauer | Ergänzung zur Strahlung, Aufteilung direkt/diffus | `TCDC:entire atmosphere`, `SUNSD:surface` |
| Bodendruck | Enthalpie, Luftdichte | `PRES:surface` |
| Bodentemperatur 0–0,1 m und 0,1–0,4 m | Quellentemperatur von Sole-Wärmepumpen und Erdkollektoren, Verluste erdverlegter Netzleitungen | `TSOIL` |
| Schneedecke | Dämmwirkung auf dem Dach, Schnee auf PV-Modulen | `WEASD`, `SNOD` |

Für das Training braucht es mehr Historie, als die eigene Sammlung zu Beginn hat. Dafür dienen der GFS-Backfill aus dem AWS-Archiv (gleiche Felder) und ERA5 als Reanalyse (3.1).

**Weitere Daten, die später hinzukommen:** Gebäudestammdaten (beheizte Fläche, Heizlast oder Wärmeverlustkoeffizient, Wärmeabgabesystem und Vorlauftemperatur, Speichergröße), Messwerte (Wärmemengenzähler, Stromzähler der Wärmepumpe, Raum-, Vorlauf- und Rücklauftemperaturen), bei Netzen die Vorlauf- und Rücklauftemperaturen und die Zahl der Abnehmer sowie Strom- und Netzentgelt-Tarife. Die API für eigene Anlagen (7.6) ist dafür die natürliche Erweiterung (neue Typen `building`, `heat_network`, `heat_pump`). Nutzungsrechtlich gilt dasselbe wie in 7.6: abgedeckt sind eigene Gebäude und selbst betriebene Netze.

### 7.9 Analyse-Jobs

Neben Training und Inferenz braucht das Projekt immer wieder Auswertungen, die eine konkrete Frage beantworten: Wie gut ist GFS je Vorlauf? Hat sich ein Modellwechsel gelohnt? Wie weit liegen zwei Quellen auseinander? Wie hätte ein Modell im letzten Winter abgeschnitten? Solche Analysen werden strukturiert beauftragt, laufen reproduzierbar und hinterlassen einen Ergebnisordner im Repo.

**Ablauf:**

```mermaid
flowchart LR
    Q["Fragestellung<br/>im Projekt"] --> J["job.yaml<br/>docs/analysen/slug/"]
    J --> R["pdb-analysis run<br/>Container, Profil analysis"]
    D[("Bronze, Silber, Gold,<br/>Beobachtungen")] -- "Lese-Schicht<br/>mit Interpolation" --> R
    R --> E["Ergebnisordner<br/>README.md, tabellen/,<br/>grafiken/, lauf.json"]
    E --> PR["PR ins Repo"]
    R --> REG[("meta.analysis_run")]
```

1. **Auftrag:** Die Frage wird als `job.yaml` im künftigen Ergebnisordner `docs/analysen/<slug>/` festgehalten (Beispiel unten). So ist schon vor dem Lauf klar und prüfbar, was verglichen wird.
2. **Lauf:** `pdb-analysis run docs/analysen/<slug>/job.yaml` im gemeinsamen Image (Compose-Profil `analysis`, `restart: "no"`), lokal, auf der VM oder als portabler Job wie in 7.7. Der Lauf liest nur und schreibt nichts in Produktionstabellen außer seinem Eintrag in `meta.analysis_run`.
3. **Ergebnis:** `README.md` mit Fragestellung, Ergebnis in Kürze, Daten und Methode, Unsicherheit und Einschränkungen, dazu `tabellen/*.csv` (Fehlerwerte, verwendete Stationen), `grafiken/*.png` bzw. `.svg`, die unveränderte `job.yaml` und `tabellen/lauf.json`. `lauf.json` hält Zeiträume, Läufe, Horizonte, Umfang (Stationen, Fehlerpaare), fehlende und zeitlich interpolierte Schritte sowie Git-Commit, Image-Version und Hashes der Eingangsobjekte fest. Braucht ein Job eigene Logik (`kind: custom`), liegt sie als `auswertung.py` im Ordner und nutzt die gemeinsamen Helfer. Große Zwischenergebnisse (z. B. Paare Prognose/Messung je Station) liegen als Parquet in Garage `analysis/<slug>/<run_id>/`, nicht im Repo.
4. **Übergabe:** Der Ergebnisordner kommt per PR ins Repo. Wiederkehrende Jobs (`schedule`) laufen über `maintenance` und aktualisieren ihren Ordner bzw. ein Grafana-Dashboard.

**Job-Definition** (Schema per Pydantic geprüft):

```yaml
id: gfs-v15-v16
title: GFS v15 gegen v16, Prognosefehler an DWD-Stationen
question: Wie stark unterscheiden sich die Fehler von v15 und v16 gegenüber Messungen, von der Analyse bis 10 Tage?
requested_by: Maulwurf
kind: verification             # verification | comparison | backtest | data_quality | custom
inputs:
  forecast:
    source: gfs_0p25
    periods:                   # Version je Lauf aus meta.model_version
      - {label: v15 2021, from: 2021-01-01, to: 2021-03-22}
      - {label: v16 2022, from: 2022-01-01, to: 2022-03-22}   # gleiche Kalendertage
      - {label: v16 2023, from: 2023-01-01, to: 2023-03-22}   # Kontrolle: Jahresschwankung
    runs: [0, 12]
    variables: [t2m, d2m, ff10, prmsl, dswrf]
  observations:
    source: dwd_cdc            # Ausweichquelle: noaa_isd
    stations: {country: DE, min_completeness: 0.8, in_all_periods: true}
method:
  horizons_h: [0, 6, 12, 24, 36, 72, 96, 240]
  time_interpolation: linear   # nur für fehlende Schritte, erst beim Lesen; none | linear | solar
  space_interpolation: bilinear
  corrections: {t2m: lapse_rate_0.0065}       # Modellhöhe -> Stationshöhe
  windows: {dswrf: mean_6h_ending, skip_night: true}
  metrics: [bias, mae, rmse, n]
  uncertainty: {rmse: bootstrap_over_runs, level: 0.95}
  group_by: [period, variable, horizon]
outputs:
  figures: [error_by_horizon]
schedule: once                 # once | monthly | on_new_model_version
```

Das Beispiel entspricht der ersten Analyse ([`docs/analysen/gfs-v15-v16/`](analysen/gfs-v15-v16/README.md)). Sie lief noch als eigenständiges Skript mit Parametern als Konstanten; die Job-Definition oben ist daraus abgeleitet und gilt für künftige Jobs.

**Grundsätze, die sich aus der ersten Analyse ergeben haben:**

- **Speichern in nativer Zeitauflösung, interpolieren beim Lesen.** Bronze und Silber halten die Schritte so, wie die Quelle sie liefert (GFS v16: stündlich bis 120 h, danach dreistündlich; ältere Archive teils nur dreistündlich). Zeitliche Interpolation geschieht ausschließlich in der Lese-Schicht (`wxread`, gemeinsam für Analyse, Features und API) und nur auf Anforderung (`time_interpolation`). Momentanwerte werden linear interpoliert. Intervallmittel und -summen werden auf die Teilintervalle verteilt, nicht linear zwischen Stützstellen gezogen. Strahlung wird nach dem Sonnenstand verteilt. Jedes Ergebnis vermerkt, welche Werte interpoliert sind, damit Fehler bei interpolierten Vorläufen nicht mit echten Stundenwerten vermischt werden.
- **Modellversionen explizit führen.** NCEP ändert Modell, Gitter und Archivpfade mit jeder GFS-Version (v15 bis 2021-03-22 06Z unter `gfs.JJJJMMTT/HH/`, v16 ab 12Z desselben Tages unter `.../HH/atmos/`). Eine Tabelle `meta.model_version` (Quelle, Version, gültig ab bis, Archivpfad, Schrittraster) ordnet jeden Lauf einer Version zu. Feature-Pipelines, Training und Analysen können so nach Version filtern bzw. die Version als Feature nutzen.
- **Versionsvergleiche über Zeiträume, nicht über Paare.** Im AWS-Archiv gibt es keinen Lauf, der in beiden Versionen vorliegt. Ein Vergleich läuft daher über Fehlerstatistiken gleicher Kalendertage in verschiedenen Jahren. Mindestens zwei Jahre der neuen Version dienen als Kontrolle: Nur was in beiden gleich vom alten Wert abweicht, gilt als Versionseffekt. Die RMSE bekommt ein Bootstrap-Intervall über die Läufe.
- **Feste Stationsmenge.** Verglichen wird nur an Stationen mit ausreichender Vollständigkeit in allen Zeiträumen (erste Analyse: mindestens 80 % der Stunden), damit sich die Stichprobe zwischen den Zeiträumen nicht ändert.
- **Variablengerecht vergleichen.** 2-m-Temperatur wird mit 0,0065 K/m auf die Stationshöhe korrigiert. Intervallgrößen wie Strahlung werden als Fenstermittel verglichen (GFS-6-h-Mittel gegen das Mittel der DWD-Stundenwerte desselben Fensters), reine Nachtfenster zählen nicht.
- **Beobachtungen sind eine eigene Quelle.** Für Verifikation braucht es Messwerte als „Wahrheit“: DWD CDC (Stationen DE, stündlich: Temperatur, Taupunkt, Wind, Druck, Globalstrahlung) und als Ausweichquelle NOAA ISD bzw. Global Hourly (SYNOP-Stationen weltweit, frei auf AWS). Sie landen im Schema `obs` (`obs.station`, `obs.value` als Hypertable mit Station, Variable, Zeit, Wert, Qualitätskennung), mit Quelle und Rang wie bei `energy.*`. Die erste Analyse hat die DWD-Daten noch selbst geladen und nur lokal zwischengespeichert.

**Erkenntnisse aus der ersten Analyse (GFS v15 gegen v16):**

| Größe | Befund | Folge |
|---|---|---|
| Temperatur, Taupunkt | v16 etwas besser (RMSE −0,1 bis −0,2 K bis 36 h), im Rahmen der Jahresschwankung, kein Bias-Sprung | keine |
| Wind 10 m | v16 Bias +0,3 bis +0,45 statt +0,15 m/s, in beiden v16-Jahren gleich; einziger klarer Versionsbruch | Windstrom-Features je Version biaskorrigieren oder Version als Merkmal |
| Luftdruck | praktisch gleich (beide etwa 0,3 hPa unter den Stationen) | keine |
| Globalstrahlung | v16 Bias +7 bis +13 statt +17 W/m², RMSE vergleichbar | keine bis gering |

Daraus folgt für das Training (7.7): v15-Läufe (etwa aus NCAR RDA d084001 ab 2015) sind brauchbar, aber nicht nahtlos. Kommen sie in die Trainingshistorie, läuft die Modellversion aus `meta.model_version` als Merkmal mit oder der Wind wird je Version korrigiert. Für den Backfill ab v16 ändert sich nichts. Eine erneute Verifikation (`schedule: on_new_model_version`) ist bei jeder neuen GFS-Version fällig.

**Was der erste Lauf vermisst hat und deshalb Teil der Architektur ist:**

- Beobachtungen in der DB (`obs`, siehe oben) statt eigener Zwischenspeicher je Analyse.
- Eine gemeinsame Lese-Schicht `wxread`: Gitter auf Station (bilinear), Höhenkorrektur, Zeitinterpolation mit Vermerk, Fenstermittel für Intervallgrößen.
- Ablage für Analyse-Zwischenstände in Garage (`analysis/<slug>/<run_id>/`, eigener Schlüssel mit Schreibrecht nur dort).
- Wiederverwendbare Statistik-Helfer im Paket `pdb_analysis` (Bias, MAE, RMSE, Bootstrap-Intervalle, Standardgrafik Fehler je Vorlauf).

**Ergänzende Tabellen:**

```sql
CREATE TABLE meta.model_version (
  source_id smallint REFERENCES meta.source, version text,
  valid_from timestamptz, valid_to timestamptz,     -- erster und letzter Lauf
  archive_prefix text, step_grid text,              -- z. B. '0-120/1,123-384/3'
  PRIMARY KEY (source_id, version)
);
CREATE TABLE meta.analysis_run (
  run_id uuid PRIMARY KEY, job_id text NOT NULL, job_sha256 text NOT NULL,
  git_commit text, image text, started_at timestamptz, finished_at timestamptz,
  status text, result_prefix text                    -- Garage analysis/<slug>/<run_id>/
);
```

Analyse-Jobs sind bewusst einfach gehalten: eine YAML-Datei, ein Befehl, ein Ordner. Eine eigene Job-Queue oder ein Workflow-System ist erst nötig, wenn regelmäßig viele Jobs parallel laufen.

### 7.10 Prognose-Dashboard (Last und Preis DE-LU)

> **Stand der Umsetzung:** Ein eigenes `prognose.json` gibt es nicht. Die Prognose steht im
> Dashboard „Prognose und Energiedaten“ (`prognose-energiedaten.json`), Bereich
> „Prognosemodelle“: Champion und Registry-Verlauf, Modellversionen mit Backtest-Kennzahlen,
> laufende Trainings, Prognosefehler D+1 im Betrieb (aus der Sicht `fcst.prediction_error`),
> Last und Preis der eigenen Modelle gegen ÜNB-Prognose bzw. Day-Ahead-Preis (Lauf vom Vortag
> 10 Uhr) sowie der gestrige Liefertag (beim Preis auch heute und morgen) mit den Prognosen
> D+1 bis D+7 für Champion und einen wählbaren Challenger. Der Ausblick −2 bis +7 Tage, der Fehlerschlauch, die Views
> `fcst.v_outlook`/`fcst.v_yesterday_envelope`, `energy.forecast_vintage`, die
> Week-Ahead-Lastprognose und `fcst.model_spread` (7.7) sind noch nicht umgesetzt; der Rest
> dieses Abschnitts beschreibt sie als Plan. Die übrigen Status-Dashboards sind „Wetter“
> (GFS-Läufe, Downloads, Bestand Bronze/Silber/Gold je Monat, CPU der Wetter-Dienste) und
> „System“ (Auslastung, Speicher, Hypertables).

Neben den Status-Dashboards (`config/grafana/dashboards/`: `prognose-energiedaten.json` „Prognose und Energiedaten“, `wetter.json` „Wetter“, `system.json` „System“, untereinander und mit der Karte verlinkt) gibt es ein eigenes Grafana-Dashboard `prognose.json` für Netzlast und Day-Ahead-Preis der Gebotszone DE-LU. Es zeigt in zwei Diagrammen, was kommt und wie gut die Prognosen gestern waren. Alle Zeiten gelten in Europe/Berlin, Tagesgrenzen liegen auf Mitternacht (Sommer- und Winterzeittage haben 23 bzw. 25 Stunden).

**Diagramm 1: Ausblick von −2 bis +7 Tage.** Zeitachse fest von vorgestern 00:00 bis zum Ende des siebten Folgetags (Grafana `now-2d/d` bis `now+7d/d`), Last in GW auf der linken, Preis in €/MWh auf der rechten Achse, eine senkrechte Linie markiert „jetzt“.

| Reihe | Quelle | Darstellung |
|---|---|---|
| Ist-Last | `energy.value` `load_actual` (ENTSO-E, Ausweich SMARD) | durchgezogene Linie bis jetzt |
| Day-Ahead-Preis | `energy.value` `da_price_de_lu` | Stufenlinie; nach der Auktion (ca. 12:45 Uhr) auch für morgen, da dann Realdaten |
| ENTSO-E-Lastprognose Day-Ahead | `load_da_forecast` | gestrichelte Linie für heute und morgen |
| ENTSO-E-Lastprognose Week-Ahead | `load_wa_forecast_min`/`_max` (Tagesminimum und -maximum) | schmales Band je Tag bis +7 |
| Eigene Prognose Last und Preis | `fcst.prediction`, jüngster Lauf des Champions | P50 als Linie über den gesamten Zeitraum, P10–P90 als Band; in der Vergangenheit der jeweils letzte Lauf vor dem Zeitpunkt |

Weitere Prognosequellen (z. B. Wind/PV der ÜNB oder Prognosen anderer Anbieter, sofern frei) kommen als eigene Reihen hinzu, sobald sie in `energy.value` stehen; das Dashboard liest alle Reihen aus `energy.series`, deren Code `_forecast` enthält, und muss dafür nicht geändert werden.

**Diagramm 2: Gestern, Ist gegen alle Prognosen.** Zeitachse fest gestern 00:00 bis 24:00 (`now-1d/d`). Das Panel wiederholt sich je Größe (Grafana-Repeat über die Variable `groesse` = Last, Preis), damit sich zwei Fehlerschläuche nicht überlagern.

- **Echtdaten** als Linie.
- **Vortagesprognose** als Linie: der letzte eigene Lauf vor Gebotsschluss am Vortag (vorgestern 12:00 Uhr), also die Prognose, auf deren Basis man für gestern hätte bieten können. Für die Last zusätzlich die ENTSO-E-Day-Ahead-Prognose als gestrichelte Linie.
- **Fehlerschlauch** über alle Prognosen, die für gestern vorlagen: alle Läufe (`issue_time` bis zum jeweiligen Zeitpunkt, also Vorläufe von wenigen Stunden bis 16 Tagen), alle parallel inferierten Modellversionen (7.7) und die ENTSO-E-Prognosen. Gezeichnet wird das Minimum bis Maximum der P50-Werte als helles Band und das 10. bis 90. Perzentil dieser Werte als dunkleres Band.
- Unter dem Diagramm stehen MAE und Bias der Vortagesprognose und der ENTSO-E-Prognose für gestern.

**Umsetzung:**

- Abfragen als Views im Schema `fcst`: `fcst.v_outlook` (Diagramm 1) und `fcst.v_yesterday_envelope` (Diagramm 2, je Größe und Zeitpunkt Ist, Vortagesprognose, Min, Max, P10, P90 und Anzahl der Prognosen). Grafana liest sie mit dem Lesebenutzer wie das Status-Dashboard.
- `energy.value` hält je Reihe und Zeitpunkt nur den letzten Wert (Upsert in `energy_ingest`). Damit der Fehlerschlauch auch frühere ENTSO-E-Stände einschließt, schreibt `energy_ingest` bei Prognosereihen zusätzlich jeden neuen Stand in `energy.forecast_vintage (series_id, ts, published, value)`, Hypertable mit Primärschlüssel `(series_id, ts, published)`. Ist-Reihen brauchen das nicht.
- Die Week-Ahead-Lastprognose von ENTSO-E (Tagesminimum und -maximum je Tag) wird als zusätzliche Reihe geladen.
- Das Dashboard liegt als JSON unter `config/grafana/dashboards/prognose.json` und wird wie die Status-Dashboards provisioniert. Quellenvermerke (ENTSO-E, SMARD) stehen im Panel-Text.

### 7.10.1 Karten-Dashboard (Cluster, Wetter, Erzeugung)

Das Dashboard `config/grafana/dashboards/karte.json` („PredictionDB Karte“) zeigt die Gebietscluster aus 7.5 und die daraus gerechneten Wetter-Features auf einer Deutschlandkarte (Grafana-Geomap). Hintergrund ist eine reine Umrisskarte mit Staats- und Bundeslandgrenzen aus Natural Earth (gemeinfrei), `config/grafana/maps/umrisse-de.geojson`, erzeugt mit `scripts/karte-umrisse.py` und per Compose nach `/usr/share/grafana/public/maps/` eingehängt; Kachelkarten brauchen keinen Zugriff und keinen Key (CARTO, die Grafana-Standardkarte, verlangt inzwischen einen). Eine eigene Wetter-Clusterung gibt es nicht: `wx-process` mittelt die GFS-Felder leistungsgewichtet auf die MaStR-Cluster.

- **Energiemenge am Prognosetag je Rasterfeld:** je Cluster mittlere Leistung (`wind_power`, `pv_power`) über die Gültigkeitszeiten des Tages (Europe/Berlin) mal Tageslänge, nach `assets.grid_weight` auf die 0,25°-Zellen verteilt und auf ein wählbares Raster (0,5/1/2°) summiert. Kreisgröße = Energiemenge, Farbe = Windanteil, Mouseover nach Erzeugungsart. Biomasse und Wasserkraft haben keine Leistungsprognose aus dem Wetter und fehlen dort.
- **Clusterung je Technologie:** Zellen aus `assets.cluster_cell` nach Cluster eingefärbt, Zentren nach installierter Leistung (`assets.capacity_daily`).
- **Wetter je Cluster:** Tagesmittel eines wählbaren Merkmals aus `feat.nwp_value` an den Clusterzentren.
- **Übertragungsnetzbetreiber (Regelzonen):** Gebiete der vier ÜNB aus dem MaStR (Regelzone am Netzanschlusspunkt jeder Einheit, `assets.zone_weight`), als 0,25°-Zellen nach der Regelzone mit der meisten Leistung eingefärbt; dazu die Energiemenge am Prognosetag je ÜNB und Erzeugungsart (Clusterenergie nach Leistungsanteil der Regelzonen aufgeteilt) und die installierte Leistung je ÜNB.
- Variablen: GFS-Lauf (Standard der jüngste verarbeitete), Prognosetag (Standard morgen), Raster, Technologie, Wettermerkmal. Die Abfragen prüft `tests/integration/test_karte_dashboard.py`.

### 7.11 Veröffentlichung trainierter Modelle (geplant)

Fertig trainierte Modelle (zuerst `de_lu_v1`: Last DE und Day-Ahead-Preis DE-LU, 7.3/7.7) sollen andere Instanzen ohne eigenes Training nutzen können. Sie werden als **eigene Modell-Releases in GHCR** veröffentlicht, getrennt von den Code-Releases, weil sich Modelle häufiger und unabhängig vom Code ändern (neuer Datenstand, Fine-Tuning, Champion-Wechsel).

Wer nur Prognosen braucht, nutzt den schlanken Stack [`predict/`](../predict/README.md): ein Container, der ein Modellpaket (`ml-train bundle`, Aufbau in `ml_train/bundle.py`, später dasselbe Paket als ORAS-Artefakt aus GHCR) lädt, die Eingänge selbst bei den freien Quellen abruft und die Prognose als Dateien und über HTTP ausgibt, ohne Datenbank und Training. Das Paket enthält die Gitter-Gewichte der Cluster, mit denen trainiert wurde; damit ist die Kompatibilitätsfrage aus 7.11.2 für Wetter-Varianten gelöst, ohne Gold-Features oder Silber zu importieren.

#### 7.11.1 Darf ein Modell veröffentlicht werden? Nutzungsbedingungen der Quellen

Geprüft am 2026-10-23 anhand der Bedingungen der Anbieter. Das ist eine technische Einordnung und keine Rechtsberatung.

| Quelle | Bedingungen | Weitergabe der Daten | Modell daraus veröffentlichen |
|---|---|---|---|
| NOAA GFS/GEFS | gemeinfrei (Werk der US-Regierung) | frei | frei |
| DWD Open Data | CC BY 4.0 (Nutzungsbedingungen, Stand Mai 2024) | mit Quellenvermerk | frei, Quellenvermerk in der Modellkarte |
| ECMWF Open Data | CC BY 4.0 | mit Quellenvermerk | frei, Quellenvermerk |
| MaStR | Datenlizenz Deutschland – Namensnennung 2.0 | mit Quellenvermerk, auch kommerziell | frei, Quellenvermerk |
| SMARD (Bundesnetzagentur) | CC BY 4.0, Quellenvermerk „Bundesnetzagentur \| SMARD.de“, gilt für den ganzen Marktdatenbereich einschließlich Großhandelspreisen | mit Quellenvermerk | frei, Quellenvermerk |
| ENTSO-E Transparency Platform | Nutzungsbedingungen 29.03.2023, Ziff. 3.1: Rechte der Primärdateninhaber (TSOs, Börsen) nicht verletzen, sonst vorher deren Zustimmung einholen; Quelle „ENTSO-E Transparency Platform“ nennen. Frei unter CC BY 4.0 ist nur die „List of Data available for free re-use“ (Stand 18.10.2023). Darauf stehen u. a. Lastprognosen (6.1.b–e), Lastflüsse, Kapazitäten und Regelenergie, **nicht** aber Ist-Last (6.1.a), Day-Ahead-Preise (12.1.d), Erzeugung (16.1) und Wind-/PV-Prognosen (14.1.d) | nur Daten der Liste | nur Datensätze der Liste; die von `energy-ingest` genutzten Reihen Preis, Ist-Last, Erzeugung und Wind/PV-Prognose sind ausgeschlossen, die Day-Ahead-Lastprognose (6.1.b) ist frei |
| Energy-Charts (Fraunhofer ISE) | API-Dokumentation v2.0: CC BY 4.0 mit Quellenangabe „Energy-Charts.info“, soweit nicht anders angegeben. `/price` nennt die Lizenz je Gebotszone im Feld `license_info`: DE-LU und 14 Nachbarzonen CC BY 4.0 von „Bundesnetzagentur \| SMARD.de“, übrige Zonen nur private und interne Nutzung (werden nicht geladen). Die Website-Bedingungen (nicht-kommerziell) gelten für die Diagramme, nicht für die API | ja, mit Quellenvermerk | frei, Quellenvermerk |
| Netztransparenz.de | weder auf der Website noch in der WebAPI-Doku (v1.14) eine Lizenz gefunden | ungeklärt | erst nach Klärung mit den ÜNB |
| GIE AGSI+ und ALSI (Gasspeicher, LNG; geprüft 2026-10-25) | keine formale Lizenz; Nutzungsregel der API: „All data published on AGSI & ALSI can be used or repackaged in any way you see fit“, Quellenangabe GIE/AGSI ist Pflicht | ja, mit Quellenvermerk „GIE AGSI+“ | frei, Quellenvermerk |
| Eurostat (Gas: `nrg_stk_gasm`, `nrg_cb_gasm`, `nrg_ti_gasm`; Öl: `nrg_ti_coifpm`, `nrg_cb_oilm`, `nrg_stk_oilm`, `nrg_ti_oilm`; Steinkohle: `nrg_cb_sffm`) | CC BY 4.0 | mit Quellenvermerk | frei, Quellenvermerk |
| Destatis GENESIS (Einfuhr Steinkohle 43511, Außenhandel 51000; geprüft 2026-10-25) | Datenlizenz Deutschland – Namensnennung 2.0; Datenabruf per API nur mit kostenlosem Konto | mit Quellenvermerk | frei, derzeit nicht angebunden (deckt sich weitgehend mit Eurostat) |
| BAFA (Rohöl- und Kohleeinfuhr, Drittlandskohlepreis; geprüft 2026-10-25) | nur Webseiten, PDF und xlsx mit wechselnden Adressen, keine API | – | nicht angebunden (kein Webseiten- oder PDF-Parsing) |
| EIA Short-Term Energy Outlook (geprüft 2026-10-25) | Prognosen sind Werk der EIA, gemeinfrei (eia.gov/about/copyrights_reuse.php); die Ist-Werte von Brent und Henry Hub in Tabelle 2 stammen von Thomson Reuters | nur die Prognosen (Monate nach dem letzten Ist-Monat) | frei, Quellenvermerk „U.S. EIA, Short-Term Energy Outlook“ |
| ENTSOG Transparency Platform (Gasflüsse, geprüft 2026-10-25) | Nutzungsbedingungen ohne offene Lizenz, Rechte vorbehalten, leistungsmindernde automatische Abrufe untersagt | ungeklärt | nicht angebunden |

**Einordnung:** Ein trainiertes Modell (Bäume bzw. Gewichte) enthält die Trainingsdaten nicht als Kopie. Seine Veröffentlichung ist daher keine Weitergabe der Zeitreihen. Das Training selbst ist Text- und Data-Mining, das § 44b UrhG für rechtmäßig zugängliche Daten erlaubt, sofern der Rechteinhaber keinen maschinenlesbaren Nutzungsvorbehalt erklärt hat; das gilt über § 87c auch für Datenbanken. Vertragliche Bedingungen eines Portals, etwa „nicht-kommerziell“ oder „vorher fragen“, bleiben trotzdem bestehen. Deshalb gilt hier die vorsichtige Regel:

1. **Ein veröffentlichtes Modell darf nur aus freigegebenen Quellen lernen.** Freigegeben sind NOAA, DWD, ECMWF Open Data, MaStR, SMARD, Energy-Charts, GIE AGSI+/ALSI, Eurostat und die Prognosen des EIA Short-Term Energy Outlook. Von ENTSO-E nur Datensätze der Freigabeliste, praktisch also nur die Lastprognose (Quelle `entsoe_last`). Netztransparenz bleibt außen vor.
2. **Mit dem Modell wird keine Datenreihe ausgeliefert**, die nicht frei weitergegeben werden darf. Das betrifft vor allem die Backtest-Datei: In die Veröffentlichung kommen nur Kennzahlen (MAE, Pinball-Loss je Ziel, Vorlauf und Jahreszeit), nicht `backtest.parquet` mit Ist-Preisen.
3. **Jede Modellkarte nennt alle Quellen mit Lizenz und Quellenvermerk** sowie den Hinweis, dass die Daten verändert (aggregiert, abgeleitet) wurden.

Umsetzung im Code (PR #36): `meta.source` hat die Felder `free` (Bedingungen erlauben freie Weitergabe) und `enabled` (im Betrieb freigegeben). Unfreie Quellen laufen nur mit `PDB_ALLOW_ENTSO_E`, `PDB_ALLOW_NETZTRANSPARENZ`, `PDB_ALLOW_EIA_REUTERS` bzw. `PDB_ALLOW_THE=true`, Vorgabe `false`; Energy-Charts (seit PR #38) und die ENTSO-E-Lastprognose (`entsoe_last`, PR #39) sind frei und laufen ohne Schalter. Übersicht für Betreiber: `docs/DATENQUELLEN-ZUGANG.md`. `energy.value_best` liest nur freigegebene Quellen, SMARD hat Rang 1. Ohne Netztransparenz rechnet die Quelle `pdb_marktwert` die Monats- und Jahresmarktwerte aus SMARD selbst. Auf einer Instanz mit Standardeinstellung ist damit jedes trainierte Modell veröffentlichbar. `pdb-model publish` prüft das trotzdem und bricht ab, wenn das Snapshot-Manifest eine Quelle mit `free = false` enthält. Die optionale Steuer-UI leitet daraus eine Lizenzklasse `frei`/`intern` je Modell ab und sperrt das Veröffentlichen von `intern`-Modellen serverseitig ([STEUER-UI.md](STEUER-UI.md) Kap. 5).

Geprüft am 2026-10-23 anhand der Liste vom 18.10.2023: Von den genutzten ENTSO-E-Reihen ist nur die Day-Ahead-Lastprognose (6.1.b, Benchmark `load_da_forecast`) frei. Offen vor der ersten Veröffentlichung bleibt, bei Netztransparenz (ÜNB) die Nutzungsbedingungen zu erfragen.

#### 7.11.2 Format und Versionierung

- **Ort:** ein Repository je Zielmarkt, `ghcr.io/the78mole/predictiondb-models/<markt>` (z. B. `…/de_lu` für Last DE und Preis DE-LU), ORAS-Artefakt mit `artifactType: application/vnd.predictiondb.model.v1`, cosign-signiert wie in 5.5.3. Alle Konfigurationen eines Marktes (`de_lu_v1`, `de_lu_v2`, Varianten) liegen im selben Repository, damit es dort genau einen `champion` gibt und Challenger und Champion im selben Ring stehen.
- **Layer:** Modelldateien wie in der Registry (`model.json` und LightGBM-Texte, später ONNX), `manifest.json`, die Konfiguration (`config/ml/<name>.yaml`), das Feature-Schema (Spaltennamen, Einheiten, Normierung) und die Modellkarte (`MODEL_CARD.md` und `model_card.json`).
- **Tags:** unveränderliche Version `<konfiguration>-<variante>-JJJJ.MM.TT-<kurzer Konfig-Hash>` (z. B. `de_lu_v2-frei-2026.10.24-3fa1c2`), dazu sprechende Tags für den Lebenslauf im Ring (Challenger, Champion, Ehrenplatz), siehe 7.11.3.
- **Modellkarte** (`MODEL_CARD.md`, Beschreibungs-Markdown des Modells), aus `manifest.json` erzeugt: Ziele und Quantile, Ausgabezeitpunkt und Vorläufe; Datenstand (Trainingszeitraum, Snapshot-Hash, letzter Ist-Wert); `FEATURES_VERSION`, `clustering_version`, `SILVER_VERSION` und Wetterquelle; Code-Version (Git-Commit, Image-Digest); Backtest-Kennzahlen gegen Baseline und TSO-Prognosen; Quellen mit Lizenz und Quellenvermerk; bekannte Grenzen (z. B. Viertelstunden-Preise erst ab Okt. 2025, 7.3). Am Ende steht die Champion-Zeitleiste des Marktes zum Zeitpunkt des Pushs (7.11.4).
- **Kompatibilität:** Ein Modell braucht dieselben Features wie beim Training. Wetter-Features hängen von der Clusterung ab (MaStR-Stand, 7.5). Eine importierende Instanz muss daher entweder dieselbe `clustering_version` haben, die mit den Gold-Features in GHCR liegt (5.5.2), oder ihre Features aus importiertem Silber mit der veröffentlichten Clusterung rechnen. `ml-predict` prüft die Versionen aus der Modellkarte und lehnt ein Modell bei Abweichung ab.

#### 7.11.3 Tags: Challenger, Champion, Ehrenplatz

Nutzer sollen an den Tags direkt sehen, welches Modell gerade Champion ist und wie die Geschichte verlief. Feste Rangnummern als Tags gibt es nicht, weil bei jedem Wechsel alle umgehängt werden müssten. Stattdessen bekommt jedes Modell **bleibende Datums-Tags** für jedes Ereignis und genau ein Tag wandert:

| Tag | Art | Wann gesetzt | Bedeutung |
|---|---|---|---|
| `<konfiguration>-<variante>-JJJJ.MM.TT-<hash>` | bleibt | beim ersten Push | feste Version, wird nie verschoben |
| `challenger-JJJJ-MM-TT` | bleibt | Tag, an dem das fertig trainierte Modell in den Ring steigt (Rolle Challenger in der Registry) | Teilnahmedatum |
| `challenger-JJJJ-MM-TT-<konfiguration>-<variante>` | bleibt | wie oben, wenn am selben Tag mehrere Challenger einsteigen | alle Challenger dieses Tages bekommen den Zusatz, z. B. `challenger-2026-10-24-de_lu_v2-frei` |
| `champion` | **wandert** | bei jeder Krönung auf das neue Modell | wer immer das beste Modell will, zieht `…/de_lu:champion` |
| `champion-JJJJ-MM-TT` | bleibt | Tag der Krönung | wird ein Ex-Champion erneut Champion, bekommt er einen weiteren `champion-…`-Tag; so bleibt jede Amtszeit sichtbar |
| `honor-JJJJ-MM-TT` | bleibt | Tag, an dem ein Champion seinen Titel verliert | Ehrenplatz, nur für Ex-Champions; bei mehreren Amtszeiten auch mehrfach |
| `retired-JJJJ-MM-TT` | bleibt | Tag, an dem ein Challenger ausscheidet, ohne je Champion gewesen zu sein | ausgeschieden; steigt dasselbe Modell später erneut ein, bekommt es einen neuen `challenger-…`-Tag |
| `baseline` | wandert | wie die Rolle in der Registry (7.7) | fester Maßstab ohne Wetter |

Beispiel für ein Modell mit zwei Amtszeiten:

```text
de_lu_v2-frei-2026.10.24-3fa1c2
challenger-2026-10-24
champion-2026-11-07     # erste Krönung
honor-2027-01-16        # von einem Wetter-Modell abgelöst
champion-2027-03-06     # Rückkehr nach Winter
champion                # aktuell
```

Die Datums-Tags sind in UTC und nennen den Tag, an dem die Registry den Wechsel vollzogen hat (`registry promote`, 7.7 und `docs/PROGNOSEMODELLE.md` Kap. 7; dieselbe Aktion bietet die optionale Steuer-UI, [STEUER-UI.md](STEUER-UI.md) Kap. 4.4). Eine Krönung setzt in einem Schritt `champion` und `champion-JJJJ-MM-TT` am Neuen und `honor-JJJJ-MM-TT` am Abgelösten, beide mit demselben Datum. Ein mit `--force` erzwungener Wechsel bekommt dieselben Tags; der Grund steht in der Zeitleiste (7.11.4). Alle Tags erfüllen die OCI-Regeln (`[A-Za-z0-9_.-]`, höchstens 128 Zeichen). Die vollständige Geschichte eines Marktes lässt sich jederzeit aus der Tag-Liste rekonstruieren (`oras repo tags ghcr.io/the78mole/predictiondb-models/de_lu`), der Abgleich mit der Registry ist damit prüfbar.

#### 7.11.4 Beschreibung, Kennzahlen und Champion-Zeitleiste

- **Kurzbeschreibung als Annotation:** Jedes Manifest trägt `org.opencontainers.image.description`, die GHCR auf der Paketseite anzeigt (höchstens 512 Zeichen). Inhalt: Konfiguration und Variante, Rolle beim Push, MAE Preis und Last für D+1 und D+2, MAE des Vortagespreises (naive Prognose) zum Vergleich, Trainingszeitraum und die Lizenz CC BY 4.0. Beispiel: `de_lu_v2/frei · Challenger seit 2026-10-24 · Preis MAE D+1 26,4 €/MWh (Vortag 28,7), D+2 … · Last MAE D+1 … GW, D+2 … GW · Training 2018-10-08–2026-10-23 · CC BY 4.0, Quellen siehe MODEL_CARD.md`.
- **Maschinenlesbare Annotationen:** dieselben Kennzahlen einzeln unter `io.predictiondb.metric.<ziel>.<vorlauf>.mae` (z. B. `io.predictiondb.metric.da_price_de_lu.d1.mae`), dazu `io.predictiondb.baseline.<ziel>.<vorlauf>.mae` für den Vortagespreis bzw. die ENTSO-E-Lastprognose, `io.predictiondb.train.from`/`.to`, `io.predictiondb.sources` (Liste mit Lizenz) sowie die Standardfelder `org.opencontainers.image.licenses=CC-BY-4.0`, `…source`, `…revision` und `…created`. So kann `pdb-model pull` oder ein Dashboard die Kennzahlen ohne Download der Gewichte lesen.
- **Modellkarte im Artefakt:** `MODEL_CARD.md` und `model_card.json` wie in 7.11.2, mit allen Quellen, Lizenzen und Quellenvermerken.
- **Champion-Zeitleiste:** Ein Manifest ist inhaltsadressiert; nachträgliche Änderungen an Annotationen oder Modellkarte würden den Digest und damit die Signatur ändern. Die Zeitleiste lebt deshalb an zwei Stellen:
  1. **Eigenes Artefakt `…/de_lu:history`** (`artifactType: application/vnd.predictiondb.model-history.v1`), das bei jedem Ereignis neu gepusht wird. Es enthält `CHAMPIONS.md` und `champions.json` mit einer Zeile je Ereignis: Datum, Ereignis (Einstieg, Krönung, Ehrenplatz, Ausscheiden), Version, Kennzahlen zum Zeitpunkt des Wechsels und bei `--force` den Grund. `CHAMPIONS.md` ist eine Markdown-Tabelle plus Mermaid-Gantt-Diagramm der Amtszeiten.
  2. **Schnappschuss in jeder neuen Modellkarte:** Beim Push hängt `pdb-model publish` die Zeitleiste bis zu diesem Tag an `MODEL_CARD.md` an. Ältere Artefakte bleiben unverändert, die aktuelle Fassung steht immer in `:history`.
- **Verlauf außerhalb von GHCR:** Jeder Champion-Wechsel erscheint zusätzlich im Release-Log des Repositorys (als Eintrag mit Link auf den Digest) und im Prognose-Dashboard (7.10) über `meta.model_event`.

#### 7.11.5 Ablauf


1. Ereignis in der Registry (7.7): neuer Challenger, Krönung oder Ausscheiden; manuell: `pdb-model publish de_lu --version <v>`.
2. Prüfung: Snapshot nur aus freigegebenen Quellen, Modellkarte vollständig, Backtest besser als die Baseline (für Champions; Challenger dürfen auch schlechter sein).
3. `oras push` mit cosign-Signatur beim ersten Auftreten der Version, danach nur noch Tags setzen bzw. versetzen wie in 7.11.3 und `:history` neu pushen (7.11.4).
4. Auf einer importierenden Instanz holt `pdb-model pull de_lu:champion` das Modell in die lokale Registry als Rolle `imported`; `ml-predict` rechnet damit wie mit einem eigenen Champion. Eigene Fine-Tuning-Läufe (7.7) können darauf aufsetzen.

**Lizenz der Modelle:** CC BY 4.0 wie die Daten (12.10, entschieden). Namensnennung: „PredictionDB (the78mole)“ plus die Quellenvermerke aus der Modellkarte.

**Zeitpunkt der Umsetzung (Entscheidung 2026-10-24):** Veröffentlichung auf GHCR und automatisches wöchentliches Neutraining mit Challenger/Champion-Wechsel werden erst eingerichtet, wenn eine belastbare GFS-Basis von mindestens drei Jahren Trainingsdaten vorliegt. Bis dahin beschreibt 7.11 nur das vereinbarte Schema; die Tags werden erst ab der ersten Veröffentlichung vergeben, frühere Wechsel in der lokalen Registry werden nicht nachgetragen.

---

## 8. Deployment auf der PVE-VM

### 8.1 VM-Größe (Startwert)

| Ressource | Wert | Begründung |
|---|---|---|
| vCPU | 6–8 | GRIB-Dekodierung, Zarr-Kompression, LightGBM/CPU-Training |
| RAM | 24–32 GB | xarray auf Europa-Ausschnitten, Postgres-Cache, Training |
| Disk | 500 GB (Daten), separat 50 GB (System) | siehe 5.4; Garage-Metadaten und Postgres idealerweise auf SSD |
| OS | Debian 13 oder Ubuntu 24.04 LTS, Docker Engine + Compose-Plugin | |

### 8.2 Compose-Dienste

```mermaid
flowchart TB
    subgraph PVE["Proxmox-Host"]
        subgraph VMX["VM: Debian/Ubuntu, Docker Engine"]
            subgraph NB["Netz backend"]
                PG[("postgres")]
                GA[("garage")]
                MIG["migrate (einmalig)"]
                ING["ingest-gfs, ingest-ecmwf,<br/>ingest-icon, ingest-misc*"]
                ENI["energy-ingest"]
                WXP["wx-process"]
                PRED["ml-predict"]
                TR["ml-train*"]
                MA["maintenance"]
            end
            subgraph NF["Netz frontend"]
                API["api"]
                MLF["mlflow"]
                GR["grafana"]
            end
            V1[/"Volume pgdata"/]
            V2[/"Volumes garage-meta, garage-data"/]
        end
    end
    PG --- V1
    GA --- V2
    U(("Browser / Clients")) --> API & MLF & GR
```

\* über Compose-Profile (`extended`, `train`) nur bei Bedarf gestartet. Die optionale Steuer-UI (Dienst `control`) läuft im eigenen Profil `control` und ist im Bild nicht enthalten ([STEUER-UI.md](STEUER-UI.md) Kap. 7).


```yaml
name: predictiondb

x-app: &app
  image: ghcr.io/the78mole/predictiondb:${TAG:-latest}
  env_file: .env
  restart: unless-stopped
  depends_on:
    postgres: {condition: service_healthy}
    garage:   {condition: service_started}
  networks: [backend]

services:
  postgres:
    image: timescale/timescaledb:latest-pg17     # Version im Lockfile pinnen
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck: {test: ["CMD", "pg_isready", "-U", "postgres"], interval: 10s}
    networks: [backend]

  garage:
    image: dxflrs/garage:v2.1.0                  # vor Einsatz aktuelle Version prüfen
    volumes:
      - ./config/garage.toml:/etc/garage.toml:ro
      - garage-meta:/var/lib/garage/meta
      - garage-data:/var/lib/garage/data
    networks: [backend]

  migrate:     {<<: *app, restart: "no", command: ["alembic", "upgrade", "head"]}

  ingest-gfs:   {<<: *app, command: ["wx-ingest", "--source", "gfs_0p25"]}
  ingest-ecmwf: {<<: *app, command: ["wx-ingest", "--source", "ecmwf_ifs"]}
  ingest-icon:  {<<: *app, command: ["wx-ingest", "--source", "icon_eu", "--source", "icon_d2"]}
  ingest-misc:  {<<: *app, command: ["wx-ingest", "--source", "gefs", "--source", "gem"], profiles: ["extended"]}

  energy-ingest: {<<: *app, command: ["energy-ingest"]}
  wx-process:    {<<: *app, command: ["wx-process"]}
  ml-predict:    {<<: *app, command: ["ml-predict"]}
  ml-train:      {<<: *app, restart: "no", command: ["ml-train"], profiles: ["train"]}
  maintenance:   {<<: *app, command: ["maintenance"]}   # Lifecycle, Backups, Konsistenzprüfungen

  api:
    <<: *app
    command: ["uvicorn", "api.main:app", "--host", "0.0.0.0"]
    networks: [backend, frontend]

  mlflow:
    image: ghcr.io/mlflow/mlflow:<version>
    command: >
      mlflow server --backend-store-uri postgresql://.../mlflow
      --artifacts-destination s3://ml-artifacts --host 0.0.0.0
    networks: [backend, frontend]

  grafana:
    image: grafana/grafana-oss:<version>
    networks: [backend, frontend]

  # optional: prometheus, reverse proxy (Caddy/Traefik) mit TLS, Uptime-Kuma

volumes: {pgdata: {}, garage-meta: {}, garage-data: {}}
networks: {backend: {}, frontend: {}}
```

Bemerkungen:

- **Ein Image für alle Python-Dienste** (Multi-Stage-Build mit `uv sync --frozen --no-dev`), unterschieden nur durch den Befehl. Das vereinfacht Updates; bei Bedarf später getrennte Images (z. B. ML-Image mit PyTorch ist groß).
- Ingest je Quellfamilie als eigener Container: Ausfall oder Sperre einer Quelle betrifft die anderen nicht; einzeln skalier- und neu startbar.
- Geheimnisse (ENTSO-E-Token, Garage-Keys, DB-Passwort) in `.env` bzw. Docker Secrets; nie im Repo. Ablage und Zugriff siehe 8.3.
- Provisionierung der VM selbst: Cloud-Init-Template auf PVE oder Ansible-Rolle (Docker installieren, Repo/Compose-Datei ausrollen, `docker compose up -d`).
- Die Betriebsdoku der konkreten Installation (Adressen, Zugänge, Befehle) liegt bewusst nicht im Repository.

**Umsetzungsstand (`compose.yaml`):**

| Dienst | Profil | Aufgabe |
|---|---|---|
| `postgres`, `garage` | – | TimescaleDB und Garage (Single Node) |
| `garage-init`, `migrate` | – | einmalig: Garage-Layout, Schlüssel, Buckets; `alembic upgrade head` |
| `ingest-gfs` | – | GFS-Live-Ingest alle 10 min (`wx-ingest gfs latest`) |
| `wx-process` | – | Silber und Gold für die Läufe der letzten 48 h |
| `energy-ingest`, `energy-revise` | – | Energiedaten alle 15 min; nachträgliche Korrekturen der Quellen (letzte 30 Tage, je Datensatz einmal täglich) |
| `mastr-snapshot` | – | MaStR-Gesamtexport einmal je Quartal (7.5) |
| `gfs-backfill` | `backfill` | GFS-Historie aus dem AWS-Archiv (Bronze) |
| `wx-process-backfill` | `backfill`, `history` | Silber und Gold für nachgeladene bzw. importierte Läufe, Laufzeiten in Prioritätsreihenfolge (`GFS_PROCESS_RUNS`) |
| `wx-import` | `history` | GFS-Silber aus GHCR (5.5) |
| `ml-train`, `ml-predict` | `ml` | Trainingsjobs mit CPU-Grenze; stündliche Inferenz beider Modelllinien |
| `grafana`, `prometheus`, `node-exporter`, `cadvisor` | – | Dashboards, Alarme per E-Mail, Metriken von VM und Containern |

Noch nicht vorhanden sind `api`, `mlflow`, `maintenance` und die Ingest-Dienste für ECMWF, ICON, GEFS und GEM. Das Image baut die CI nach jeder grünen Pipeline auf `main`; ein systemd-Timer rollt neue Stände auf der VM aus (`deploy/systemd/`, `scripts/vm-update.sh`, README).

### 8.3 Geheimnisse

Zugangsdaten (DB-Passwort, Garage-Schlüssel, API-Token der Quellen, SMTP, GHCR-Token) gehören
nie ins Repository. Empfohlen ist ein Secret-Store als Quelle der Wahrheit, z. B. OpenBao oder
HashiCorp Vault (KV v2), ersatzweise ein Passwortmanager.

| Ort | Inhalt | Zweck |
|---|---|---|
| Secret-Store | `postgres_password`, Garage-RPC-Secret und Admin-Token, S3-Schlüssel, Token der Quellen (ENTSO-E, EIA, GIE, Netztransparenz), SMTP-Passwort, GHCR-Token | Quelle der Wahrheit |
| `.env` auf dem Host (chmod 600, gitignored) | dieselben Werte als `POSTGRES_PASSWORD`, `GARAGE_*`, `PDB_S3_*`, `PDB_ENTSOE_TOKEN`, `PDB_SMTP_PASSWORD`, `WX_EXCHANGE_TOKEN` … | Laufzeit für Docker Compose; aus dem Secret-Store erzeugt |

Regeln:

- Im Secret-Store stehen nur echte Geheimnisse (Passwörter, Token, ggf. Benutzernamen). Hosts,
  URLs, Ports und Alarmadressen sind normale Werte in der `.env` (Vorlage `.env.example`).
- Werte nur per stdin schreiben und nur als Variable lesen, nie ausgeben oder ins Log schreiben.
- Neue Geheimnisse zuerst in den Secret-Store, dann die `.env` daraus erzeugen und den
  betroffenen Dienst neu starten.
- Der Host braucht kein Token für den Secret-Store, solange die `.env` von einem
  Verwaltungsrechner aus geschrieben wird. Bei der Cloud-Migration (Kapitel 9) ersetzt ein Agent
  mit eigenem, nur lesendem Token oder der Secret-Store des Anbieters diesen Schritt.
- Welche Quellen einen Schlüssel brauchen und wie man ihn bekommt:
  [DATENQUELLEN-ZUGANG.md](DATENQUELLEN-ZUGANG.md).
- Die optionale Steuer-UI legt keine Geheimnisse im Klartext in der Datenbank ab; sie zeigt nur
  an, ob ein Wert per `.env` gesetzt ist ([STEUER-UI.md](STEUER-UI.md) Kap. 6).

---

## 9. Migrationspfad in die Cloud

| Baustein | PVE (jetzt) | Cloud (später) | Was sich ändert |
|---|---|---|---|
| Objektspeicher | Garage, 1 Knoten | AWS S3, Cloudflare R2, Hetzner Object Storage oder Garage-Cluster mit 3 Knoten | nur Endpoint/Keys |
| Datenbank | TimescaleDB-Container | Timescale Cloud, oder Postgres auf k8s (CloudNativePG + Timescale) | Verbindungsstring; Migration per `pg_dump`/logischer Replikation |
| Dienste | Docker Compose | Kubernetes (Helm/Kustomize aus derselben Image-Basis) oder ECS/Cloud Run | Deployment-Manifeste |
| Scheduling | APScheduler in-process | unverändert, oder k8s CronJobs | optional |
| Training | CPU auf VM | GPU-Instanzen on demand | nur Laufumgebung |

```mermaid
flowchart LR
    subgraph NOW["Heute: PVE-VM"]
        C1["Docker Compose"]
        G1[("Garage, 1 Knoten")]
        P1[("TimescaleDB-Container")]
    end
    subgraph LATER["Später: Cloud"]
        C2["Kubernetes / Cloud Run"]
        G2[("S3, R2, Hetzner<br/>oder Garage-Cluster")]
        P2[("Managed Postgres<br/>mit Timescale")]
    end
    C1 -- "gleiches Image,<br/>Manifeste statt Compose" --> C2
    G1 -- "rclone sync,<br/>nur Endpoint/Keys ändern" --> G2
    P1 -- "pg_dump bzw.<br/>logische Replikation" --> P2
```

Regeln, die die Migration einfach halten: keine lokalen Dateipfade für Nutzdaten (alles über S3), keine Host-Abhängigkeiten außer Volumes für Postgres/Garage, Konfiguration ausschließlich über Umgebungsvariablen und YAML im Image bzw. ConfigMap, Zustandsführung in Postgres (Container sind austauschbar).

Datenabfluss beachten: Bei S3-Diensten mit Egress-Kosten (AWS) sollten Training und Daten im selben Rechenzentrum liegen; R2 hat keine Egress-Gebühren.

---

## 10. Betrieb und Qualität

- **Logging:** JSON über stdout, gesammelt per Docker-Logtreiber bzw. Loki.
- **Metriken:** Läufe pro Quelle vollständig/verspätet/fehlgeschlagen, Bytes je Lauf, Latenz zwischen Veröffentlichung und Ablage, Prognosefehler je Horizont.
- **Alarme:** „Kein vollständiger GFS-Lauf seit 12 h“, „ENTSO-E-Daten älter als 2 h“, „Garage-Füllstand > 80 %“. Umgesetzt sind Grafana-Alarme per E-Mail (`config/grafana/provisioning/alerting/`): Format einer Datenquelle geändert, Abruf scheitert seit 24 h, Reihe veraltet, Ist-Last korrigiert, Partition zu 90 % belegt.
- **Tests:** Unit-Tests für `.idx`-Parser und Range-Merging mit aufgezeichneten Fixtures; Integrationstest gegen echte Endpunkte als separater, manuell ausgelöster CI-Job (nicht in jedem Lauf, wegen Rate-Limits).
- **Prognose-Dashboard:** Ausblick Last und Preis DE-LU von −2 bis +7 Tage und Vortagesvergleich mit Fehlerschlauch (7.10).
- **Monitoring bleibt Grafana.** Die optionale Steuer-UI dient nur dem Steuern (Quellen, Input-Sets, Trainingsläufe, Champion/Challenger) und verlinkt für Verläufe auf Grafana und MLflow ([STEUER-UI.md](STEUER-UI.md)).
- **Analysen:** Auswertungen laufen als Analyse-Jobs mit Ergebnisordner unter `docs/analysen/` (7.9).
- **Lizenzen:** Quellenvermerke (DWD, ECMWF CC BY 4.0, SMARD/Energy-Charts CC BY 4.0, MaStR DL-DE BY 2.0) in API-Antworten und Dashboards mitführen.

---

## 11. Umsetzungsphasen

| Phase | Inhalt | Ergebnis |
|---|---|---|
| **0 – Spike** | Herbie vs. eigenes `gribkit` für GFS + ECMWF; Größen pro Lauf messen; Java-Code sichten | Entscheidung Bibliothek, belastbares Mengengerüst |
| **1 – Fundament** | uv-Workspace, Compose mit Postgres/Timescale + Garage, Alembic-Schema, `core`-Paket, CI (Lint, Tests, Image-Build) | lauffähiger leerer Stack |
| **2 – GFS-Ingest** | GFS mit NOMADS + AWS-Fallback, Bronze, Zustandsführung, Backfill ab 2021 aus AWS | durchgehende GFS-Historie |
| **3 – Energiedaten** | ENTSO-E, SMARD/Energy-Charts, MaStR-Quartalsabruf und Bestandsrekonstruktion, Feiertage | Zielgrößen, Anlagenbestand + Kalender in Gold |
| **4 – Processing** | Silber-Zarr, Anlagen-Cluster und Gitter-Gewichte (7.5), Wind-/PV-/Temperatur-Features | Feature-Tabellen |
| **4b – Anlagenprognose** | API für eigene PV-, Wind- und Wasserkraftanlagen (7.6): Stammdaten, Messwert-Upload, pvlib/windpowerlib, Abflussmodell, Kalibrierung | erste nutzbare Prognose ohne trainiertes Netz |
| **5 – Baseline-ML** | LightGBM für Last und Preis, Evaluierungs-Framework, MLflow | Referenzwerte |
| **6 – NN** | TFT/TiDE zweistufig, Quantile, Inferenzdienst, Grafana | erste produktive Prognose |
| **7 – Weitere Quellen** | ECMWF IFS/AIFS, ICON-EU/D2, GEFS, GEM; Redundanz-Features | robuster Betrieb |
| **7b – Datenaustausch** | `wx-publish` (Silber nach GHCR, Backfill monatsweise, cosign), `wx-import` als Quelle `ghcr` mit Fallback auf eigenen Download; vorher Quantisierung Silber gegen Bronze prüfen und GHCR-Grenzen verifizieren (5.5) | neue Instanzen ohne GFS-Download |
| **7c – Modell-Releases** | Prüfung `meta.source.free` beim Veröffentlichen, `pdb-model publish/pull`, Modellkarte aus `manifest.json`, Tags und Champion-Zeitleiste (7.11); erst ab drei Jahren GFS-Trainingsdaten | Modelle ohne eigenes Training nutzbar |
| **8 – API & Härtung** | FastAPI, Auth, Alarme, Backups, Cloud-Probelauf | betriebsbereit |
| **später – Steuer-UI** | optionales Dashboard mit FastAPI-Backend `control`, ENV-Vorrang, Lizenzklasse je Modell, später Keycloak/OIDC ([STEUER-UI.md](STEUER-UI.md) Kap. 9) | Bedienung ohne CLI, abschaltbar |
| **später – Wärme/Kälte** | Wärme-/Kältelast für Gebäude und Netze, Strombedarf von Wärmepumpen, Betriebsoptimierung (7.8); Architektur noch offen | geplant |

```mermaid
flowchart LR
    P0["0 Spike"] --> P1["1 Fundament"] --> P2["2 GFS-Ingest"]
    P1 --> P3["3 Energiedaten<br/>+ MaStR"]
    P2 --> P4["4 Processing"]
    P3 --> P4
    P4 --> P5["5 Baseline-ML"] --> P6["6 NN"]
    P4 --> P4B["4b Anlagenprognose<br/>PV/Wind/Wasser"]
    P4 --> P7B["7b Datenaustausch<br/>GHCR"]
    P6 --> P7C["7c Modell-Releases"]
    P2 --> P7["7 Weitere Quellen"]
    P7 --> P6
    P6 --> P8["8 API & Härtung"]
```

---

## 12. Offene Punkte und Entscheidungen

1. ~~Lizenz~~ **Entschieden (2026-10-20):** Business Source License 1.1 (`LICENSE`). Produktive Nutzung nur intern, für eigene Energieanlagen an eigenen Standorten; ausgeschlossen sind SaaS für Dritte, Steuerung fremder Anlagen, kommerzielles Flottenmanagement und der Verkauf von Prognosedaten. Change Date vier Jahre nach Veröffentlichung, Change License AGPL-3.0-or-later. Lizenzgeber: Daniel (the78mole) Glaser; ein späterer Übergang der kommerziellen Lizenzierung auf die Bizzmark UG (haftungsbeschränkt) ist offen.
2. ~~Verhältnis zu PredictionDB~~ **Entschieden (2026-10-20):** Das Projekt wird im Repo `the78mole/PredictionDB` fortgeführt. Dieser Entwurf liegt dort als `docs/ARCHITEKTUR.md` und ersetzt die Teile der `SPECIFICATION.md` v0.1, die ihm widersprechen (MariaDB, Gitterwerte als Zeilen, Celery).
3. **Kommerzielle Nutzung?** Entscheidet, ob Open-Meteo produktiv genutzt werden darf und ob bestimmte Lizenzauflagen (ENTSO-E-Bedingungen) relevant werden.
4. **Zielauflösung des Preises:** 15 min (aktueller Markt) oder Stunde (einfacher, längere homogene Historie)? Vorschlag: 15 min als Ausgabe, Training mit Stundenmitteln vor Okt. 2025 und Upsampling-Kennzeichnung.
5. **Ausschnitt:** Nur DE oder Mitteleuropa (Nachbarländer beeinflussen Preis über Kuppelflüsse)? Vorschlag: 44–58° N, 0–20° E.
6. **GPU:** Steht auf dem PVE-Host eine GPU für Passthrough bereit? Ohne lokale GPU startet das Training auf CPU, Hyperparameter-Suchen laufen auf stundenweise gemieteten GPUs (7.7).
7. Zu verifizierende Detailwerte vor der Implementierung: exakte NOMADS-Rate-Limits, aktuelle Vorhaltezeiten auf opendata.dwd.de und data.ecmwf.int, Beginn der Archive auf AWS, aktuelle Garage- und TimescaleDB-Versionen, Zugang und Lizenz von GloFAS/EFAS sowie Schnittstellen der Pegelportale der Länder.
8. **Anlagen-API (7.6):** Nur eigene Anlagen oder auch Anlagen weiterer Nutzer? Mehrere Nutzer bzw. fremde Anlagen fallen nicht unter den Additional Use Grant (nur intern). Lizenz der Turbinen-Leistungskurven (oedb) vor Übernahme prüfen.
9. ~~Datenlizenz der GHCR-Artefakte~~ **Entschieden (2026-10-23):** Die aufbereiteten Daten in GHCR (Silber, Bronze, Features, 5.5) stehen unter CC BY 4.0, mit Weitergabe der Quellenvermerke; die BSL gilt nur für den Code. Namensraum: unter dem Konto `the78mole` (`ghcr.io/the78mole/predictiondb-data/*` und `…/predictiondb-models/<markt>`), nicht unter der Bizzmark UG.
10. ~~Lizenz veröffentlichter Modelle~~ **Entschieden (2026-10-23):** CC BY 4.0, wie die Daten. Die Modelle sollen frei sein; Voraussetzung ist das Training nur aus freien Quellen (7.11.1). Offen bleiben die Bedingungen von Netztransparenz; bis dahin rechnet `pdb_marktwert` die Marktwerte selbst.
11. **Steuer-UI (optional):** Konzept in [STEUER-UI.md](STEUER-UI.md), noch nicht umgesetzt. Festgelegt: ENV-Variablen haben Vorrang, die UI ist zuschaltbar und das System läuft ohne sie; Grafana bleibt für Monitoring; Auth zunächst zurückgestellt, später Keycloak per OIDC mit Rollen aus Claims. Entschieden (2026-10-25): Unfreie Quellen werden ausschließlich per ENV (`PDB_ALLOW_*`) freigegeben, die UI kann sie nur ausschließen. Offen: SPA-Framework, eine UI je Instanz oder instanzübergreifend, Zeitpunkt der Auth-Pflicht (STEUER-UI Kap. 10).
