# PredictionDB — Spezifikation

**Version:** 0.1.0  
**Status:** Entwurf  
**Letzte Änderung:** 2026-07-21

---

## 1. Projektübersicht

PredictionDB ist eine lokale Datenbank-Lösung zur Sammlung, Speicherung und Verwaltung von Wetter- und Prognosedaten aus externen Quellen. Das System besteht aus einem MariaDB-Datenbank-Container und einem Worker-Container, der Daten abruft, verarbeitet und in die Datenbank übernimmt.

### 1.1 Ziele

- **Zentrale Speicherung** aller Wetterdaten an einem Ort
- **Automatisierte Datenerfassung** aus mehreren externen Quellen
- **Historische Analyse** von Wetterdaten über Zeiträume
- **Schnittstelle** für das beECO Prediction-System
- **Offline-fähig** — vollständig lokal auf dem eigenen Server

### 1.2 Architektur-Überblick

```
┌────────────────────────────────────────────────────────────────────┐
│                    predictiondb (Docker Compose)                   │
│                                                                    │
│  ┌──────────────────────┐       ┌──────────────────────────────┐   │
│  │  Worker Container    │       │      MariaDB Container       │   │
│  │                      │       │                              │   │
│  │  ┌────────────────┐  │       │  ┌────────────────────────┐  │   │
│  │  │  Plugin        │──┼───────┼─►│  Tables:               │  │   │
│  │  │  (Fetcher +    │  │       │  │  - pdid_registry       │  │   │
│  │  │   Parser, je   │  │       │  │  - datapoints_int      │  │   │
│  │  │   Datenquelle, │  │       │  │  - datapoints_float    │  │   │
│  │  │   YAML-Config) │  │       │  │  - datapoints_varchar  │  │   │
│  │  └────────────────┘  │       │  └────────────────────────┘  │   │
│  │       │              │       │                              │   │
│  │       ▼              │       │  Volume: /var/lib/mysql      │   │
│  │  ┌────────────────┐  │       │                              │   │
│  │  │  Uploader      │──┼───────┼─►INSERT/UPDATE Datenpunkte    │   │
│  │  └────────────────┘  │       │  (PDID, Timestamp, Offset,   │   │
│  └──────────────────────┘       │   Value)                     │   │
│                                 └──────────────────────────────┘   │
│                                 ┌──────────────────────────────┐   │
│                                 │   PredictionDB API           │   │
│                                 │   (optional, v2)             │   │
│                                 └──────────────────────────────┘   │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Datenquellen (extern, über Internet) — je eine als Plugin   │   │
│  │                                                             │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │   │
│  │  │ NOAA NOMADS  │  │ DWD          │  │ Open-Meteo API    │  │   │
│  │  │ (GRIB2)      │  │ (GRIB/TXT)   │  │ (JSON)            │  │   │
│  │  └──────────────┘  └──────────────┘  └───────────────────┘  │   │
│  │                                                             │   │
│  │  ┌──────────────┐  ┌──────────────┐                         │   │
│  │  │ ECMWF        │  │ KML/RWTH     │                         │   │
│  │  │ (HRES/IFS)   │  │ (JSON)       │                         │   │
│  │  └──────────────┘  └──────────────┘                         │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. Datenmodell

### 2.1 Konzept

Die Fachlogik je Datenquelle (Wettermodell, API, Sensor, ...) steckt vollständig in einem **Plugin**. Ein Plugin bringt seine eigene Konfiguration als YAML-Datei mit (Fetch-Parameter, Intervall, Zuordnung der Rohdaten auf Datenpunkte) und liefert der Datenbank ausschließlich fertige, generische **Datenpunkte**. Die Datenbank selbst kennt keine Quellen, Modelle, Läufe oder Gitterpunkte mehr — sie speichert nur Zeitreihenwerte. Neue Plugins oder Änderungen an bestehenden erfordern damit keine Schema-Änderung.

**Zeitpunkt statt Lauf+Offset:** GRIB-Dateien liefern typischerweise einen Vorhersage-Lauf-Zeitpunkt (`run_time`) und einen Offset dazu (0 = Analyse, +1h, +2h, ...). Beispiel: Lauf `2026-07-21 12:00:00` mit Offsets `0`, `+1h`, `+2h`, ... Damit Abfragen nicht bei jedem Zugriff `run_time + offset` nachrechnen müssen, wird das beim Import bereits aufgelöst: gespeichert wird der **Gültigkeitszeitpunkt** `timestamp = run_time + offset`. Der `offset` bleibt zusätzlich erhalten, damit ersichtlich ist, wie weit im Voraus ein Wert vorhergesagt wurde — dadurch können für denselben `timestamp` mehrere Vorhersagen aus unterschiedlichen Läufen (verschiedene Offsets) koexistieren, z.B. für Genauigkeits-Analysen über die Vorlaufzeit. `run_time` lässt sich bei Bedarf über `timestamp - offset` zurückrechnen und muss daher nicht separat gespeichert werden.

**Negativer Offset = echte Messung:** Ein negativer Offset (z.B. `-1`) markiert keine Vorhersage, sondern einen tatsächlich gemessenen Wert (z.B. Sensor- oder Stationsmessung) für den jeweiligen `timestamp` — im Unterschied zu Offset `0`, der weiterhin die Modell-Analyse (also einen vom Modell berechneten, nicht real gemessenen Wert) bezeichnet. Dadurch können Analyse, Vorhersage und reale Messung für denselben Zeitpunkt nebeneinander gespeichert und verglichen werden (z.B. zur Bewertung der Vorhersagegüte).

**PDID (Plugin-Datapoint-ID):** Jeder Datenpunkt-Typ, den ein Plugin liefert (z.B. "2m-Temperatur von noaa_gdas"), bekommt eine PDID. Sie legt implizit Plugin, Datenpunkt-Name und Einheit fest — die Zuordnung steht im Register ([2.1.1](#211-pdid_registry--register-der-datenpunkte)) und wird beim Start des Plugins aus dessen YAML-Konfiguration synchronisiert.

Ein Datenpunkt besteht damit nur noch aus diesen Werten:

| Feld | Bedeutung |
|---|---|
| `pdid` | Verweis auf Plugin + Datenpunkt-Name + Einheit (siehe Register) |
| `latitude`, `longitude` | Ort, für den der Wert gilt |
| `timestamp` | Gültigkeitszeitpunkt des Werts |
| `offset` | Vorlaufzeit der Vorhersage zum `timestamp` (0 = Analyse, negativ = echte Messung) |
| `value` | Mess-/Vorhersagewert |

Jeder Wert ist also einem Ort zugeordnet — bei gegitterten Modellen (GRIB) liefert ein Plugin je Zeitschritt mehrere Zeilen (eine je Gitterpunkt/Ausschnitt), bei Punkt-APIs (z.B. Open-Meteo) i.d.R. nur eine feste `latitude`/`longitude` je Plugin-Instanz.

Da Plugins Werte unterschiedlichen Typs liefern (Zahlen, Text, ...), gibt es **je Value-Type eine eigene Tabelle** statt einer generischen `value`-Spalte.

#### 2.1.1 `pdid_registry` — Register der Datenpunkte

Ordnet jede PDID einem Plugin, einem Datenpunkt-Namen, einer Einheit und dem Value-Type (und damit der Ziel-Tabelle aus 2.1.2) zu. Wird von jedem Plugin beim Start aus seiner YAML-Konfiguration synchronisiert, nicht manuell gepflegt.

```sql
CREATE TABLE pdid_registry (
    pdid INT AUTO_INCREMENT PRIMARY KEY,
    plugin_code VARCHAR(50) NOT NULL,                    -- z.B. 'noaa_gdas', 'dwd_icon', 'open_meteo'
    datapoint_code VARCHAR(100) NOT NULL,                -- z.B. 'temperature_2m', 'wind_speed_10m'
    unit VARCHAR(20) NOT NULL,                           -- z.B. 'K', 'm/s', 'hPa'
    value_type ENUM('int', 'float', 'varchar') NOT NULL, -- legt die Ziel-Tabelle fest
    description VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_pdid (plugin_code, datapoint_code)
);
```

#### 2.1.2 `datapoints_*` — Werte-Tabellen (eine je Value-Type)

Gleiche Struktur je Typ, nur die `value`-Spalte unterscheidet sich. Weitere Typen (z.B. `datapoints_bool`, `datapoints_text`) lassen sich nach demselben Muster ergänzen.

```sql
CREATE TABLE datapoints_int (
    pdid INT NOT NULL,
    latitude DECIMAL(9,6) NOT NULL,
    longitude DECIMAL(9,6) NOT NULL,
    timestamp DATETIME NOT NULL,           -- Gültigkeitszeitpunkt
    offset_seconds INT NOT NULL DEFAULT 0, -- Vorlaufzeit zum timestamp (0 = Analyse, < 0 = echte Messung)
    value INT NOT NULL,
    PRIMARY KEY (pdid, latitude, longitude, timestamp, offset_seconds),
    FOREIGN KEY (pdid) REFERENCES pdid_registry(pdid),
    INDEX idx_datapoints_int_timestamp (timestamp)
);

CREATE TABLE datapoints_float (
    pdid INT NOT NULL,
    latitude DECIMAL(9,6) NOT NULL,
    longitude DECIMAL(9,6) NOT NULL,
    timestamp DATETIME NOT NULL,
    offset_seconds INT NOT NULL DEFAULT 0,
    value DOUBLE NOT NULL,
    PRIMARY KEY (pdid, latitude, longitude, timestamp, offset_seconds),
    FOREIGN KEY (pdid) REFERENCES pdid_registry(pdid),
    INDEX idx_datapoints_float_timestamp (timestamp)
);

CREATE TABLE datapoints_varchar (
    pdid INT NOT NULL,
    latitude DECIMAL(9,6) NOT NULL,
    longitude DECIMAL(9,6) NOT NULL,
    timestamp DATETIME NOT NULL,
    offset_seconds INT NOT NULL DEFAULT 0,
    value VARCHAR(255) NOT NULL,
    PRIMARY KEY (pdid, latitude, longitude, timestamp, offset_seconds),
    FOREIGN KEY (pdid) REFERENCES pdid_registry(pdid),
    INDEX idx_datapoints_varchar_timestamp (timestamp)
);
```

Die zusammengesetzte Primärschlüssel `(pdid, latitude, longitude, timestamp, offset_seconds)` erlaubt genau eine Zeile je (Datenpunkt, Ort, Gültigkeitszeitpunkt, Vorlaufzeit) — ein `INSERT ... ON DUPLICATE KEY UPDATE` überschreibt also eine erneut gelieferte Vorhersage mit demselben Offset für denselben Ort und Gültigkeitszeitpunkt, behält aber Vorhersagen mit anderem Offset oder an anderem Ort.

#### 2.1.3 Beispiel

GRIB-Lauf am `2026-07-21 12:00:00`, Parameter `temperature_2m` von `noaa_gdas` (PDID `1`) für einen Gitterpunkt bei Berlin, Offsets `0`, `+1h`, `+2h`, plus eine echte Messung für `13:00:00` von einem Mess-Plugin (PDID `2`, Offset `-1`) an derselben Stelle:

| pdid | latitude | longitude | timestamp | offset_seconds | value |
|---|---|---|---|---|---|
| 1 | 52.520000 | 13.410000 | 2026-07-21 12:00:00 | 0 | 21.4 |
| 1 | 52.520000 | 13.410000 | 2026-07-21 13:00:00 | 3600 | 20.9 |
| 1 | 52.520000 | 13.410000 | 2026-07-21 14:00:00 | 7200 | 20.1 |
| 2 | 52.520000 | 13.410000 | 2026-07-21 13:00:00 | -1 | 21.1 |

Abfrage "aktuellste Vorhersage je Ort und Gültigkeitszeitpunkt" (kleinster nicht-negativer Offset zuerst): `... WHERE pdid = 1 AND latitude = 52.52 AND longitude = 13.41 AND offset_seconds >= 0 ORDER BY timestamp, offset_seconds`. Abfrage "nur Analysen": `... WHERE pdid = 1 AND offset_seconds = 0`. Abfrage "nur echte Messungen": `... WHERE pdid = 2 AND offset_seconds < 0`.

### 2.2 ER-Diagramm

```
┌────────────────────┐        ┌───────────────────────┐
│  pdid_registry      │  1   n │  datapoints_int        │
│                     │───────►│  pdid (PK, FK)         │
│  pdid (PK)          │        │  latitude (PK)         │
│  plugin_code        │        │  longitude (PK)        │
│  datapoint_code     │        │  timestamp (PK)        │
│  unit               │        │  offset_seconds (PK)   │
│  value_type         │        │  value                 │
│                     │        └───────────────────────┘
│                     │  1   n ┌───────────────────────┐
│                     │───────►│  datapoints_float      │
│                     │        │  pdid (PK, FK)         │
│                     │        │  latitude (PK)         │
│                     │        │  longitude (PK)        │
│                     │        │  timestamp (PK)        │
│                     │        │  offset_seconds (PK)   │
│                     │        │  value                 │
│                     │        └───────────────────────┘
│                     │
│                     │  1   n ┌───────────────────────┐
│                     │───────►│  datapoints_varchar    │
└────────────────────┘        │  pdid (PK, FK)         │
                               │  latitude (PK)         │
                               │  longitude (PK)        │
                               │  timestamp (PK)        │
                               │  offset_seconds (PK)   │
                               │  value                 │
                               └───────────────────────┘
```

### 2.3 Retention

Es gibt keine `data_retention_rules`-Tabelle mehr. Aufbewahrungsfristen sind Teil der Worker-Konfiguration (siehe [4.1](#41-configyaml-global)) und wirken direkt auf die `datapoints_*`-Tabellen (`DELETE ... WHERE timestamp < NOW() - INTERVAL max_days DAY`).

---

## 3. Datenquellen-Spezifikation

Jede der folgenden Datenquellen wird als eigenständiges Plugin implementiert (`plugin_code` in Klammern). Die hier aufgeführten Parameter definieren, welche Datenpunkte das jeweilige Plugin in seiner YAML-Konfiguration deklariert und damit in `pdid_registry` registriert (siehe [2.1.1](#211-pdid_registry--register-der-datenpunkte)).

### 3.1 NOAA NOMADS (GDAS/Global Forecast System) — Plugin `noaa_gdas`

**Basis-URL:** `https://nomads.ncep.noaa.gov/cgi-bin/filter_gdas1.pl`

**GRIB2-Dateien:**
- `gdas1.grb2` — Hauptdatei
- Enthält: Temperatur, Wind (U/V), Druck, Feuchtigkeit, Strahlung

**Parameter (Auswahl) → Datenpunkt-Code (`datapoint_code`) in `pdid_registry`:**
| GRIB ID | Beschreibung | datapoint_code | unit | value_type |
|---------|-------------|-----------------|------|------------|
| 11 | Bodentemperatur | temperature_surface | K | float |
| 12 | Lufttemperatur auf verschiedenen Niveaus | temperature_2m | K | float |
| 130 | Geopotentialhöhe | geopotential_height | gpm | float |
| 33 | U-Wind-Komponente | wind_u_10m | m/s | float |
| 35 | V-Wind-Komponente | wind_v_10m | m/s | float |
| 132 | Relative Luftfeuchte | relative_humidity | % | float |
| 203 | Akkumulierter Niederschlag | precipitation | kg/m² | float |

**Aufruf-Beispiel:**
```
https://nomads.ncep.noaa.gov/cgi-bin/filter_gdas1.pl?file=gdas1.grb2&var_...
```

**Aktualisierungsintervall:** Alle 6 Stunden (00Z, 06Z, 12Z, 18Z)

**Verfügbare Daten:** Bis zu 16 Tage im Voraus

---

### 3.2 DWD (Deutscher Wetterdienst) — ICON

**Basis-URL:** `https://opendata.dwd.de/climate_environment/CMS/`

**GRIB2-Dateien:**
- ICON-EU: 0.02° Auflösung (~2 km)
- ICON-D2: 0.02° Auflösung (Deutschland)

**Verfügbare Daten:** Bis zu 96 Stunden im Voraus

---

### 3.3 ECMWF — HRES

**API-Endpunkt:** `https://api.ecmwf.int/v1`

**Modell:** High Resolution Ensemble Forecast (HRES)

**Aktualisierungsintervall:** Alle 12 Stunden

**Daten:** Bis zu 10 Tage im Voraus

---

### 3.4 Open-Meteo API

**API-Endpunkt:** `https://api.open-meteo.com/v1/forecast`

**Format:** JSON

**Vorteile:** Kein Login notwendig, CORS-freundlich

**Parameter:** Temperatur, Wind, Bewölkung, Niederschlag, etc.

---

## 4. Worker-Konfiguration

Die Worker-Konfiguration ist zweigeteilt: eine globale `config.yaml` (Datenbank, aktive Plugins, Retention, Logging) und je Plugin eine eigenständige YAML-Datei (Fetch-Parameter, Datenpunkt-Definitionen). Dadurch bringt jedes Plugin sein Wissen über Fetch und Datenpunkte selbst mit — die globale Konfiguration muss bei neuen Plugins nur um einen Eintrag in `plugins.enabled` ergänzt werden.

### 4.1 config.yaml (global)

```yaml
# PredictionDB Worker Konfiguration

database:
  host: mariadb
  port: 3306
  name: predictiondb
  user: ${DB_USER:-prediction}
  password: ${DB_PASSWORD:-prediction}
  pool_size: 5

# Plugins
plugins:
  directory: /app/plugins     # Verzeichnis mit einem Unterordner je Plugin
  enabled:
    - noaa_gdas
    - dwd_icon
    - open_meteo

# Bereinigung (wirkt auf alle datapoints_*-Tabellen)
retention:
  enabled: true
  max_days: 365                # Wie lange Datenpunkte behalten?
  cleanup_cron: "0 3 * * *"     # Täglicher Bereinigungs-Job um 3:00 Uhr

# Logging
logging:
  level: INFO          # DEBUG, INFO, WARNING, ERROR
  file: /var/log/predictiondb/worker.log
  max_file_size_mb: 100
  backup_count: 5
```

### 4.2 Plugin-Konfiguration (Beispiel `plugins/noaa_gdas/config.yaml`)

```yaml
plugin:
  code: noaa_gdas
  name: "NOAA GDAS / GFS"
  enabled: true
  interval_hours: 6

fetch:
  type: grib2
  url: "https://nomads.ncep.noaa.gov/cgi-bin/filter_gdas1.pl"
  params:
    file: "gdas1.grb2"
    var_T: "on"
    var_UGRD: "on"
    var_VGRD: "on"
    level_type: "1"
    min_lat: "-90"
    max_lat: "90"
    min_lon: "0"
    max_lon: "360"
  timeout_seconds: 300
  batch_size: 1000

# Datenpunkte, die dieses Plugin liefert.
# Wird beim Start des Plugins in pdid_registry synchronisiert (siehe 2.1.1).
datapoints:
  - code: temperature_2m
    grib_param_id: 12
    unit: K
    value_type: float
  - code: wind_u_10m
    grib_param_id: 33
    unit: m/s
    value_type: float
  - code: wind_v_10m
    grib_param_id: 35
    unit: m/s
    value_type: float
  - code: relative_humidity
    grib_param_id: 132
    unit: "%"
    value_type: float
```

### 4.3 Worker-Architektur

```
┌─────────────────────────────────────────────────────────────┐
│                    Worker Container                          │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Scheduler (Celery Beat)                   │ │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐ │ │
│  │  │ Plugin  │ │ Plugin  │ │ Plugin  │ │ Cleanup     │ │ │
│  │  │ noaa_   │ │ dwd_    │ │ open_   │ │ Job         │ │ │
│  │  │ gdas(6h)│ │ icon(6h)│ │ meteo(1h│ │ (daily)     │ │ │
│  │  └────┬────┘ └────┬────┘ └────┬────┘ └──────┬──────┘ │ │
│  └───────┼───────────┼───────────┼──────────────┼────────┘ │
│          │           │           │              │          │
│          ▼           ▼           ▼              │          │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Worker Pool (Celery Workers)           │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐    │  │
│  │  │ Plugin     │  │ Plugin     │  │ Plugin     │    │  │
│  │  │ #1 (GRIB)  │  │ #2 (GRIB)  │  │ #3 (JSON)  │    │  │
│  │  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘    │  │
│  └────────┼───────────────┼───────────────┼────────────┘  │
│           │               │               │               │
│           ▼               ▼               │               │
│  ┌─────────────────────────────────────────────────────┐  │
│  │       Plugin-interner Fetch & Parse (je Plugin)     │  │
│  │                                                     │  │
│  │  GRIB-Datei ──► Parser ──► Mapping auf PDID/Value   │  │
│  │   JSON ───────► Parser ──► Mapping auf PDID/Value   │  │
│  └──────────────────────────┬──────────────────────────┘  │
│                             │                              │
│                             ▼                              │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Database Uploader                      │  │
│  │                                                     │  │
│  │  Batch INSERT in datapoints_* (1000-10000 Zeilen)   │  │
│  │  ON DUPLICATE KEY UPDATE (pdid, lat, lon, ts, offset)│ │
│  └──────────────────────────┬──────────────────────────┘  │
│                             │                              │
│                             ▼                              │
│                    MariaDB Container                       │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. docker-compose.yml

```yaml
services:
  # ============================================================
  # MariaDB — Zentrale Datenbank
  # ============================================================
  mariadb:
    image: mariadb:11.4
    container_name: predictiondb-mariadb
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: ${DB_ROOT_PASSWORD:-prediction_root}
      MYSQL_DATABASE: predictiondb
      MYSQL_USER: prediction
      MYSQL_PASSWORD: ${DB_PASSWORD:-prediction}
    ports:
      - "3306:3306"
    volumes:
      - predictiondb-data:/var/lib/mysql
      - ./docker/mysql/init.sql:/docker-entrypoint-initdb.d/01-init.sql
      - ./docker/mysql/my.cnf:/etc/mysql/conf.d/my.cnf
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - predictiondb-net
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: '1.0'

  # ============================================================
  # Worker — Datenabfrage und -speicherung
  # ============================================================
  worker:
    build:
      context: .
      dockerfile: docker/worker/Dockerfile
    container_name: predictiondb-worker
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - predictiondb-config:/app/config
      - predictiondb-logs:/var/log/predictiondb
      - predictiondb-raw:/data/raw:ro   # Falls GRIB-Dateien gespeichert werden
    depends_on:
      mariadb:
        condition: service_healthy
    networks:
      - predictiondb-net
    deploy:
      resources:
        limits:
          memory: 4G
          cpus: '2.0'

  # ============================================================
  # Cleanup — Datenbereinigung (optional)
  # ============================================================
  cleanup:
    build:
      context: .
      dockerfile: docker/worker/Dockerfile
    container_name: predictiondb-cleanup
    restart: unless-stopped
    command: ["python", "-m", "predictiondb.cleanup"]
    env_file:
      - .env
    volumes:
      - predictiondb-logs:/var/log/predictiondb
    depends_on:
      mariadb:
        condition: service_healthy
    networks:
      - predictiondb-net

  # ============================================================
  # API — Zugriff auf die Daten (optional, v2)
  # ============================================================
  # api:
  #   build:
  #     context: .
  #     dockerfile: docker/api/Dockerfile
  #   container_name: predictiondb-api
  #   restart: unless-stopped
  #   ports:
  #     - "8000:8000"
  #   env_file:
  #     - .env
  #   depends_on:
  #     mariadb:
  #       condition: service_healthy
  #   networks:
  #     - predictiondb-net

volumes:
  predictiondb-data:
    driver: local
  predictiondb-config:
    driver: local
  predictiondb-logs:
    driver: local
  predictiondb-raw:
    driver: local

networks:
  predictiondb-net:
    driver: bridge
```

---

## 6. Docker-Struktur

```
PredictionDB/
├── SPECIFICATION.md          ← Diese Datei
├── docker-compose.yml
├── .env
├── .gitignore
├── README.md
├── pyproject.toml
│
├── src/
│   └── predictiondb/
│       ├── __init__.py
│       ├── config.py          # Lädt globale config.yaml
│       ├── database.py        # DB-Verbindung (SQLAlchemy)
│       ├── models.py          # SQLAlchemy ORM-Modelle (pdid_registry, datapoints_*)
│       ├── plugin.py          # Plugin-Basisklasse & Registry-Sync (YAML → pdid_registry)
│       │
│       ├── plugins/
│       │   ├── noaa_gdas/
│       │   │   ├── plugin.py    # Fetcher + Parser + Mapping auf Datenpunkte
│       │   │   └── config.yaml  # Fetch-Parameter & Datenpunkt-Definitionen
│       │   ├── dwd_icon/
│       │   │   ├── plugin.py
│       │   │   └── config.yaml
│       │   ├── ecmwf/
│       │   │   ├── plugin.py
│       │   │   └── config.yaml
│       │   └── open_meteo/
│       │       ├── plugin.py
│       │       └── config.yaml
│       │
│       ├── parsers/            # Gemeinsame Parser-Utilities (von Plugins genutzt)
│       │   ├── grib2.py        # GRIB2-Parser (pygrib / CMC)
│       │   └── json_parser.py  # JSON-Parser
│       │
│       ├── uploader.py         # Batch-Upload zu MariaDB
│       └── cleanup.py          # Retention: alte Datenpunkte löschen
│
├── tests/
│   ├── test_config.py
│   ├── test_plugins/
│   ├── test_parsers/
│   └── test_uploader.py
│
├── docker/
│   ├── worker/
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   ├── api/                   # (optional, v2)
│   │   ├── Dockerfile
│   │   └── entrypoint.sh
│   └── mysql/
│       ├── init.sql           # Tabellen erstellen (pdid_registry, datapoints_*)
│       └── my.cnf             # MariaDB Konfiguration
│
├── config/
│   ├── config.yaml            # Globale Worker-Konfiguration (DB, aktive Plugins, Retention)
│   └── config.example.yaml    # Beispiel-Konfiguration
│
├── scripts/
│   ├── migrate.py             # Datenbank-Migrationen
│   └── seed.py                # Testdaten einspielen
│
└── docs/
    └── api.md                 # API-Dokumentation (v2)
```

---

## 7. SQLAlchemy Models

```python
# src/predictiondb/models.py

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Column, Integer, String, Boolean, Float,
    DateTime, ForeignKey, Enum as SAEnum,
    UniqueConstraint, Index, DECIMAL, TIMESTAMP
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ValueType(str, Enum):
    INT = "int"
    FLOAT = "float"
    VARCHAR = "varchar"


# ============================================================
# Tabellen-Modelle
# ============================================================

class PDIDRegistry(Base):
    """Register der Datenpunkte — wird von jedem Plugin beim Start aus
    seiner YAML-Konfiguration synchronisiert, nicht manuell gepflegt."""
    __tablename__ = 'pdid_registry'

    pdid = Column(Integer, primary_key=True, autoincrement=True)
    plugin_code = Column(String(50), nullable=False)
    datapoint_code = Column(String(100), nullable=False)
    unit = Column(String(20), nullable=False)
    value_type = Column(SAEnum(ValueType), nullable=False)
    description = Column(String(255))
    is_active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('plugin_code', 'datapoint_code', name='uq_pdid'),
    )


class _DatapointMixin:
    """Gemeinsame Spalten aller datapoints_*-Tabellen."""
    pdid = Column(Integer, ForeignKey('pdid_registry.pdid'), primary_key=True)
    latitude = Column(DECIMAL(9, 6), primary_key=True)
    longitude = Column(DECIMAL(9, 6), primary_key=True)
    timestamp = Column(DateTime, primary_key=True)          # Gültigkeitszeitpunkt
    offset_seconds = Column(Integer, primary_key=True, default=0)  # 0 = Analyse, < 0 = echte Messung


class DatapointInt(_DatapointMixin, Base):
    __tablename__ = 'datapoints_int'

    value = Column(Integer, nullable=False)

    __table_args__ = (
        Index('idx_datapoints_int_timestamp', 'timestamp'),
    )


class DatapointFloat(_DatapointMixin, Base):
    __tablename__ = 'datapoints_float'

    value = Column(Float, nullable=False)

    __table_args__ = (
        Index('idx_datapoints_float_timestamp', 'timestamp'),
    )


class DatapointVarchar(_DatapointMixin, Base):
    __tablename__ = 'datapoints_varchar'

    value = Column(String(255), nullable=False)

    __table_args__ = (
        Index('idx_datapoints_varchar_timestamp', 'timestamp'),
    )
```

---

## 8. Implementierungs-Phasen

### Phase 1: Fundament (Woche 1)
- [ ] `docker-compose.yml` erstellen
- [ ] MariaDB mit init.sql aufsetzen
- [ ] SQLAlchemy Models implementieren
- [ ] config.yaml Struktur definieren
- [ ] Grundlegende Worker-Architektur

### Phase 2: Plugins (Woche 2-3)
- [ ] NOAA GDAS Plugin implementieren (Fetch + Parser + Datenpunkt-Mapping)
- [ ] GRIB2-Parser (pygrib)
- [ ] Open-Meteo JSON-Parser
- [ ] pdid_registry-Sync aus Plugin-YAML

### Phase 3: Storage & Scheduling (Woche 4)
- [ ] Batch-Upload zu MariaDB
- [ ] Scheduler (Celery Beat oder APScheduler)
- [ ] Konfiguration für mehrere Plugins

### Phase 4: Cleanup & Monitoring (Woche 5)
- [ ] Datenbereinigung (retention rules)
- [ ] Logging und Error-Handling
- [ ] Health-Checks

### Phase 5: API (optional, v2)
- [ ] FastAPI-Endpoint für Datenabfrage
- [ ] Authentifizierung
- [ ] Swagger-Dokumentation

---

## 9. Konfiguration (.env)

```bash
# Datenbank
DB_ROOT_PASSWORD=prediction_root_secure
DB_PASSWORD=prediction_secure
DB_USER=prediction
DB_NAME=predictiondb
DB_HOST=mariadb
DB_PORT=3306

# Worker
WORKER_LOG_LEVEL=INFO
WORKER_PARALLELISM=2

# Plugins
PLUGIN_NOAA_GDAS_ENABLED=true
PLUGIN_DWD_ICON_ENABLED=true
PLUGIN_OPEN_METEO_ENABLED=true
PLUGIN_ECMWF_ENABLED=false

# API (optional)
API_ENABLED=false
API_HOST=0.0.0.0
API_PORT=8000
```

---

## 10. Nächste Schritte

1. ✅ Git-Repository erstellen
2. ✅ SPECIFICATION.md erstellen
3. 🔲 `docker-compose.yml` erstellen
4. 🔲 SQL-Init-Skript erstellen
5. 🔲 Worker-Python-Paket strukturieren
6. 🔲 NOAA GDAS Plugin implementieren
7. 🔲 GRIB2-Parser integrieren
8. 🔲 Scheduler einrichten
9. 🔲 Tests schreiben
10. 🔲 Dokumentation vervollständigen

---

*Diese Spezifikation dient als Grundlage und sollte entsprechend den Anforderungen angepasst und erweitert werden.*
