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
┌─────────────────────────────────────────────────────────────────────┐
│                     predictiondb (Docker Compose)                   │
│                                                                     │
│  ┌──────────────────────┐       ┌──────────────────────────────┐   │
│  │  Worker Container    │       │      MariaDB Container       │   │
│  │                      │       │                              │   │
│  │  ┌────────────────┐  │       │  ┌────────────────────────┐  │   │
│  │  │  Fetcher       │──┼───────┼─►│  Tables:               │  │   │
│  │  │  (External     │  │       │  │  - grib_messages       │  │   │
│  │  │   Sources)     │  │       │  │  - weather_observations│  │   │
│  │  └────────────────┘  │       │  │  - forecasts           │  │   │
│  │       │              │       │  │  - metadata            │  │   │
│  │       ▼              │       │  └────────────────────────┘  │   │
│  │  ┌────────────────┐  │       │                              │   │
│  │  │  Parser        │  │       │  Volume: /var/lib/mysql      │   │
│  │  │  (GRIB/JSON)   │  │       │                              │   │
│  │  └────────────────┘  │       └──────────────────────────────┘   │
│  │       │              │                                          │
│  │       ▼              │                                          │
│  │  ┌────────────────┐  │                                          │
│  │  │  Normalizer    │  │                                          │
│  │  │  & Transformer │  │                                          │
│  │  └────────────────┘  │                                          │
│  │       │              │                                          │
│  │       ▼              │                                          │
│  │  ┌────────────────┐  │                                          │
│  │  │  Uploader      │──┼──► INSERT/UPDATE into MariaDB         │   │
│  │  └────────────────┘  │                                          │
│  └──────────────────────┘       ┌──────────────────────────────┐   │
│                                   │   PredictionDB API           │   │
│                                   │   (optional, v2)             │   │
│                                   └──────────────────────────────┘   │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Datenquellen (extern, über Internet)                        │   │
│  │                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │   │
│  │  │ NOAA NOMADS  │  │ DWD          │  │ Open-Meteo API    │  │   │
│  │  │ (GRIB2)      │  │ (GRIB/TXT)   │  │ (JSON)            │  │   │
│  │  └──────────────┘  └──────────────┘  └───────────────────┘  │   │
│  │                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐                         │   │
│  │  │ ECMWF        │  │ KML/RWTH     │                         │   │
│  │  │ (HRES/IFS)   │  │ (JSON)       │                         │   │
│  │  └──────────────┘  └──────────────┘                         │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Datenmodell

### 2.1 Tabellen-Entwurf

#### 2.1.1 `sources` — Datenquellen-Metadaten

```sql
CREATE TABLE sources (
    id INT AUTO_INCREMENT PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE,           -- z.B. 'noaa_gdas', 'dwd_icon'
    name VARCHAR(100) NOT NULL,                 -- Anzeige-Name
    type ENUM('grib2', 'grib1', 'json', 'csv', 'xml') NOT NULL,
    base_url TEXT,                               -- API-Endpunkt / Server-URL
    update_interval_hours INT DEFAULT 6,         -- Wie oft aktualisieren?
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

#### 2.1.2 `models` — Wettermodelle

```sql
CREATE TABLE models (
    id INT AUTO_INCREMENT PRIMARY KEY,
    source_id INT NOT NULL,
    code VARCHAR(50) NOT NULL,                  -- z.B. 'gdas1', 'icon-eu'
    name VARCHAR(100) NOT NULL,                 -- z.B. 'GDAS 1 Degree'
    resolution_degrees DECIMAL(4,2),            -- räumliche Auflösung
    valid_from TIMESTAMP,                       -- Modell-Gültigkeit beginnt
    valid_until TIMESTAMP,                      -- Modell-Gültigkeit endet
    run_time TIMESTAMP,                         -- Modell-Laufzeit (Startzeit)
    is_current BOOLEAN DEFAULT FALSE,
    FOREIGN KEY (source_id) REFERENCES sources(id)
);
```

#### 2.1.3 `parameters` — Meteorologische Parameter

```sql
CREATE TABLE parameters (
    id INT AUTO_INCREMENT PRIMARY KEY,
    grib_param_id INT,                          -- GRIB-Parameter-Id (falls GRIB)
    code VARCHAR(50) NOT NULL UNIQUE,           -- z.B. 'temperature', 'wind_u'
    name VARCHAR(100) NOT NULL,                 -- Anzeige-Name
    unit VARCHAR(20) NOT NULL,                  -- z.B. 'K', 'm/s', 'hPa'
    category VARCHAR(50),                       -- z.B. 'temperature', 'wind', 'pressure'
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### 2.1.4 `forecast_runs` — Vorhersage-Läufe

```sql
CREATE TABLE forecast_runs (
    id INT AUTO_INCREMENT PRIMARY KEY,
    model_id INT NOT NULL,
    run_time TIMESTAMP NOT NULL,                -- Wann wurde das Modell berechnet?
    valid_from TIMESTAMP NOT NULL,              -- Ab wann sind die Prognosen gültig?
    valid_until TIMESTAMP NOT NULL,             -- Bis wann?
    file_path VARCHAR(500),                     -- Lokaler Dateipfad (falls GRIB gespeichert)
    file_size_bytes BIGINT,                     -- Dateigröße
    status ENUM('downloading', 'parsing', 'completed', 'failed') DEFAULT 'downloading',
    error_message TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(id)
);
```

#### 2.1.5 `grid_points` — Gitterpunkte

```sql
CREATE TABLE grid_points (
    id INT AUTO_INCREMENT PRIMARY KEY,
    latitude DECIMAL(9,6) NOT NULL,             -- Breitengrad
    longitude DECIMAL(9,6) NOT NULL,            -- Längengrad
    elevation_m DECIMAL(8,2),                   -- Höhe über NN (optional)
    grid_index INT NOT NULL,                    -- Index im Raster (0..N)
    is_active BOOLEAN DEFAULT TRUE,
    UNIQUE KEY unique_grid (latitude, longitude)
);
```

#### 2.1.6 `observations` — Beobachtungen/Werte

```sql
CREATE TABLE observations (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    forecast_run_id INT NOT NULL,               -- Zu welchem Lauf gehört der Wert?
    parameter_id INT NOT NULL,                  -- Welcher Parameter?
    grid_point_id INT NOT NULL,                 -- An welchem Ort?
    valid_time TIMESTAMP NOT NULL,              -- Wann ist der Wert gültig?
    value DECIMAL(10,4),                        -- Messwert / Prognosewert
    quality_flag TINYINT DEFAULT 0,             -- 0=ok, 1=approximiert, 2=fehlerhaft
    forecast_hour INT,                          -- Stunden-Vorhersage (0=start, 24=+1Tag)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (forecast_run_id) REFERENCES forecast_runs(id),
    FOREIGN KEY (parameter_id) REFERENCES parameters(id),
    FOREIGN KEY (grid_point_id) REFERENCES grid_points(id),
    UNIQUE KEY unique_observation (forecast_run_id, parameter_id, grid_point_id, valid_time)
);
```

#### 2.1.7 `observations_summary` — Aggregierte Daten (optional, Performance)

```sql
CREATE TABLE observations_summary (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    forecast_run_id INT NOT NULL,
    parameter_id INT NOT NULL,
    valid_date DATE NOT NULL,                   -- Datum (kein Zeitstempel)
    hour_of_day INT NOT NULL,                   -- Stunde (0-23)
    mean_value DECIMAL(10,4),                   -- Mittelwert über alle Grid-Points
    min_value DECIMAL(10,4),                    -- Minimum
    max_value DECIMAL(10,4),                    -- Maximum
    std_dev DECIMAL(10,4),                      -- Standardabweichung
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (forecast_run_id) REFERENCES forecast_runs(id),
    FOREIGN KEY (parameter_id) REFERENCES parameters(id),
    UNIQUE KEY unique_summary (forecast_run_id, parameter_id, valid_date, hour_of_day)
);
```

#### 2.1.8 `data_retention_rules` — Aufbewahrungsregeln

```sql
CREATE TABLE data_retention_rules (
    id INT AUTO_INCREMENT PRIMARY KEY,
    category VARCHAR(50) NOT NULL,              -- z.B. 'high_resolution', 'global'
    max_age_days INT NOT NULL DEFAULT 365,      -- Wie lange speichern?
    aggregation_level ENUM('raw', 'hourly', 'daily') DEFAULT 'raw',
    is_active BOOLEAN DEFAULT TRUE
);
```

### 2.2 ER-Diagramm

```
┌──────────┐     1      ┌──────────┐     1      ┌──────────────┐
│ sources  │──────┐     │ models   │──────┐     │ forecast_    │
│          │      │     │          │      │     │ runs         │
│ id       │      │     │ id       │      │     │ id           │
│ code     │      └────►│ source_  │      └────►│ model_id     │
│ name     │          │ id       │           │ run_time       │
│ type     │          │ code     │           │ valid_from     │
└──────────┘          │ name     │           │ status         │
                      └──────────┘           └──────┬───────┘
                                                    │
                                                    │ 1
                    ┌───────────────────────────────┘
                    │
                    ▼ n
┌──────────┐     ┌──────────────┐     ┌──────────────────────┐
│ params   │     │ observations │     │ observations_summary │
│          │     │              │     │                      │
│ id       │◄────│ forecast_    │     │ id                   │
│ code     │     │ run_id       │     │ forecast_run_id      │
│ name     │     │ parameter_   │     │ parameter_id         │
│ unit     │     │ id           │     │ valid_date           │
└──────────┘     │ grid_point_  │     │ hour_of_day          │
                 │ id           │     │ mean_value           │
                 │ valid_time   │     └──────────────────────┘
                 │ value        │
┌──────────┐     │ quality_     │
│grid_     │     │ flag         │
│points    │     └──────────────┘
│          │
│ id       │
│ latitude │
│ longitude│
│ elevation│
└──────────┘
```

### 2.3 Indexe

```sql
-- observations: Schneller Zugriff nach Lauf, Parameter und Ort
CREATE INDEX idx_obs_run ON observations (forecast_run_id);
CREATE INDEX idx_obs_param ON observations (parameter_id);
CREATE INDEX idx_obs_grid ON observations (grid_point_id);
CREATE INDEX idx_obs_valid ON observations (valid_time);
CREATE INDEX idx_obs_run_param ON observations (forecast_run_id, parameter_id);
CREATE INDEX idx_obs_run_param_grid ON observations (forecast_run_id, parameter_id, grid_point_id);

-- forecast_runs: Schneller Zugriff nach Modell und Laufzeit
CREATE INDEX idx_run_model ON forecast_runs (model_id);
CREATE INDEX idx_run_time ON forecast_runs (run_time);
CREATE INDEX idx_run_status ON forecast_runs (status);

-- grid_points: Räumliche Abfragen
CREATE INDEX idx_grid_lat ON grid_points (latitude);
CREATE INDEX idx_grid_lon ON grid_points (longitude);
CREATE SPATIAL INDEX idx_grid_location ON grid_points (latitude, longitude);
```

---

## 3. Datenquellen-Spezifikation

### 3.1 NOAA NOMADS (GDAS/Global Forecast System)

**Basis-URL:** `https://nomads.ncep.noaa.gov/cgi-bin/filter_gdas1.pl`

**GRIB2-Dateien:**
- `gdas1.grb2` — Hauptdatei
- Enthält: Temperatur, Wind (U/V), Druck, Feuchtigkeit, Strahlung

**Parameter (Auswahl):**
| GRIB ID | Beschreibung |
|---------|-------------|
| 11 | Bodentemperatur (K) |
| 12 | Lufttemperatur auf verschiedenen Niveaus (K) |
| 130 | Geopotentialhöhe (gpm) |
| 33 | U-Wind-Komponente (m/s) |
| 35 | V-Wind-Komponente (m/s) |
| 132 | Relative Luftfeuchte (%) |
| 203 | Akkumulierter Niederschlag (kg/m²) |

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

### 4.1 config.yaml

```yaml
# PredictionDB Worker Konfiguration

database:
  host: mariadb
  port: 3306
  name: predictiondb
  user: ${DB_USER:-prediction}
  password: ${DB_PASSWORD:-prediction}
  pool_size: 5

# Datenquellen
sources:
  - name: noaa_gdas
    type: grib2
    enabled: true
    interval_hours: 6
    fetch:
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
    parse:
      timeout_seconds: 300
      batch_size: 1000

  - name: dwd_icon
    type: grib2
    enabled: true
    interval_hours: 6
    fetch:
      url: "https://opendata.dwd.de/climate_environment/CMS/..."
    parse:
      timeout_seconds: 600
      batch_size: 5000

  - name: open_meteo
    type: json
    enabled: true
    interval_hours: 1
    fetch:
      url: "https://api.open-meteo.com/v1/forecast"
      params:
        latitude: "52.52"       # Berlin (anpassbar)
        longitude: "13.41"
        hourly: >-
          temperature_2m,
          relative_humidity_2m,
          wind_speed_10m,
          precipitation,
          cloud_cover,
          surface_pressure
        timezone: "Europe/Berlin"
    parse:
      batch_size: 100

# Speicherung
storage:
  # GRIB-Dateien lokal speichern?
  store_raw_files: false
  raw_files_dir: /data/raw

  # Prognose-Parameter, die gespeichert werden
  save_parameters:
    - temperature
    - wind_u
    - wind_v
    - pressure
    - humidity
    - radiation
    - precipitation

# Bereinigung
retention:
  enabled: true
  # Alte Prognosen löschen (Standard: 365 Tage)
  max_days: 365
  # Täglicher Bereinigungs-Job um 3:00 Uhr
  cleanup_cron: "0 3 * * *"

# Logging
logging:
  level: INFO          # DEBUG, INFO, WARNING, ERROR
  file: /var/log/predictiondb/worker.log
  max_file_size_mb: 100
  backup_count: 5
```

### 4.2 Worker-Architektur

```
┌─────────────────────────────────────────────────────────────┐
│                    Worker Container                          │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Scheduler (Celery Beat)                   │ │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐ │ │
│  │  │ NOAA    │ │ DWD     │ │ Open-   │ │ Cleanup     │ │ │
│  │  │ Fetcher │ │ Fetcher │ │ Meteo   │ │ Job         │ │ │
│  │  │ (6h)    │ │ (6h)    │ │ (1h)    │ │ (daily)     │ │ │
│  │  └────┬────┘ └────┬────┘ └────┬────┘ └──────┬──────┘ │ │
│  └───────┼───────────┼───────────┼──────────────┼────────┘ │
│          │           │           │              │          │
│          ▼           ▼           ▼              │          │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Worker Pool (Celery Workers)           │  │
│  │  ┌────────────┐  ┌────────────┐  ┌────────────┐    │  │
│  │  │ Fetcher    │  │ Fetcher    │  │ Fetcher    │    │  │
│  │  │ #1 (GRIB)  │  │ #2 (GRIB)  │  │ #3 (JSON)  │    │  │
│  │  └─────┬──────┘  └─────┬──────┘  └─────┬──────┘    │  │
│  └────────┼───────────────┼───────────────┼────────────┘  │
│           │               │               │               │
│           ▼               ▼               │               │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Parse & Transform Pipeline             │  │
│  │                                                     │  │
│  │  GRIB-Datei ──► Parser ──► Normalizer ──► Validator│  │
│  │   JSON ───────► Parser ──► Normalizer ──► Validator│  │
│  └──────────────────────────┬──────────────────────────┘  │
│                             │                              │
│                             ▼                              │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Database Uploader                      │  │
│  │                                                     │  │
│  │  Batch INSERT (1000-10000 records)                  │  │
│  │  ON DUPLICATE KEY UPDATE                            │  │
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
│       ├── config.py          # Lädt config.yaml
│       ├── database.py        # DB-Verbindung (SQLAlchemy)
│       ├── models.py          # SQLAlchemy ORM-Modelle
│       │
│       ├── sources/
│       │   ├── base.py        # Basisklasse für Datenquellen
│       │   ├── noaa_gdas.py   # NOAA GDAS Fetcher
│       │   ├── dwd_icon.py    # DWD ICON Fetcher
│       │   ├── ecmwf.py       # ECMWF Fetcher
│       │   └── open_meteo.py  # Open-Meteo Fetcher
│       │
│       ├── parsers/
│       │   ├── base.py        # Basisklasse für Parser
│       │   ├── grib2.py       # GRIB2-Parser (pygrib / CMC)
│       │   └── json_parser.py # JSON-Parser
│       │
│       ├── normalizer.py      # Daten-Normalisierung
│       ├── validator.py       # Daten-Validierung
│       ├── uploader.py        # Batch-Upload zu MariaDB
│       └── cleanup.py         # Alte Daten löschen
│
├── tests/
│   ├── test_config.py
│   ├── test_sources/
│   ├── test_parsers/
│   ├── test_normalizer.py
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
│       ├── init.sql           # Tabellen erstellen
│       └── my.cnf             # MariaDB Konfiguration
│
├── config/
│   ├── config.yaml            # Haupt-Konfiguration
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

from datetime import datetime, date, time
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Float,
    DateTime, Date, Time, ForeignKey, Enum as SAEnum,
    UniqueConstraint, Index, DECIMAL, BIGINT, TINYINT,
    Numeric, TIMESTAMP
)
from sqlalchemy.orm import relationship, DeclarativeBase


class Base(DeclarativeBase):
    pass


class SourceType(str, Enum):
    GRIB2 = "grib2"
    GRIB1 = "grib1"
    JSON = "json"
    CSV = "csv"
    XML = "xml"


class RunStatus(str, Enum):
    DOWNLOADING = "downloading"
    PARSING = "parsing"
    COMPLETED = "completed"
    FAILED = "failed"


class QualityFlag(int, Enum):
    OK = 0
    APPROXIMATED = 1
    ERROR = 2


# ============================================================
# Tabellen-Modelle
# ============================================================

class Source(Base):
    __tablename__ = 'sources'

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    type = Column(SAEnum(SourceType), nullable=False)
    base_url = Column(Text)
    update_interval_hours = Column(Integer, default=6)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    models = relationship("Model", back_populates="source")
    forecast_runs = relationship("ForecastRun", back_populates="source")


class Model(Base):
    __tablename__ = 'models'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(Integer, ForeignKey('sources.id'), nullable=False)
    code = Column(String(50), nullable=False)
    name = Column(String(100), nullable=False)
    resolution_degrees = Column(DECIMAL(4, 2))
    valid_from = Column(DateTime)
    valid_until = Column(DateTime)
    run_time = Column(DateTime)
    is_current = Column(Boolean, default=False)

    source = relationship("Source", back_populates="models")
    forecast_runs = relationship("ForecastRun", back_populates="model")


class Parameter(Base):
    __tablename__ = 'parameters'

    id = Column(Integer, primary_key=True, autoincrement=True)
    grib_param_id = Column(Integer)
    code = Column(String(50), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    unit = Column(String(20), nullable=False)
    category = Column(String(50))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ForecastRun(Base):
    __tablename__ = 'forecast_runs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Integer, ForeignKey('models.id'), nullable=False)
    run_time = Column(DateTime, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_until = Column(DateTime, nullable=False)
    file_path = Column(String(500))
    file_size_bytes = Column(BIGINT)
    status = Column(SAEnum(RunStatus), default=RunStatus.DOWNLOADING)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    model = relationship("Model", back_populates="forecast_runs")
    observations = relationship("Observation", back_populates="run")


class GridPoint(Base):
    __tablename__ = 'grid_points'

    id = Column(Integer, primary_key=True, autoincrement=True)
    latitude = Column(DECIMAL(9, 6), nullable=False)
    longitude = Column(DECIMAL(9, 6), nullable=False)
    elevation_m = Column(DECIMAL(8, 2))
    grid_index = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint('latitude', 'longitude', name='uq_grid'),
    )


class Observation(Base):
    __tablename__ = 'observations'

    id = Column(BIGINT, primary_key=True, autoincrement=True)
    forecast_run_id = Column(Integer, ForeignKey('forecast_runs.id'), nullable=False)
    parameter_id = Column(Integer, ForeignKey('parameters.id'), nullable=False)
    grid_point_id = Column(Integer, ForeignKey('grid_points.id'), nullable=False)
    valid_time = Column(DateTime, nullable=False)
    value = Column(DECIMAL(10, 4))
    quality_flag = Column(TINYINT, default=0)
    forecast_hour = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)

    run = relationship("ForecastRun", back_populates="observations")
    parameter = relationship("Parameter")
    grid_point = relationship("GridPoint")

    __table_args__ = (
        UniqueConstraint(
            'forecast_run_id', 'parameter_id', 'grid_point_id', 'valid_time',
            name='uq_observation'
        ),
        Index('idx_obs_run', 'forecast_run_id'),
        Index('idx_obs_param', 'parameter_id'),
        Index('idx_obs_grid', 'grid_point_id'),
        Index('idx_obs_valid', 'valid_time'),
        Index('idx_obs_run_param', 'forecast_run_id', 'parameter_id'),
        Index('idx_obs_run_param_grid', 'forecast_run_id', 'parameter_id', 'grid_point_id'),
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

### Phase 2: Datenquellen (Woche 2-3)
- [ ] NOAA GDAS Fetcher implementieren
- [ ] GRIB2-Parser (pygrib)
- [ ] Open-Meteo JSON-Parser
- [ ] Daten-Normalisierung

### Phase 3: Storage & Scheduling (Woche 4)
- [ ] Batch-Upload zu MariaDB
- [ ] Scheduler (Celery Beat oder APScheduler)
- [ ] Konfiguration für mehrere Quellen

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

# Datenquellen
SOURCE_NOAA_ENABLED=true
SOURCE_DWD_ENABLED=true
SOURCE_OPEN_METEO_ENABLED=true
SOURCE_ECMWF_ENABLED=false

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
6. 🔲 NOAA GDAS Fetcher implementieren
7. 🔲 GRIB2-Parser integrieren
8. 🔲 Scheduler einrichten
9. 🔲 Tests schreiben
10. 🔲 Dokumentation vervollständigen

---

*Diese Spezifikation dient als Grundlage und sollte entsprechend den Anforderungen angepasst und erweitert werden.*
