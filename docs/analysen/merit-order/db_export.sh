#!/usr/bin/env bash
# Datenbank-Teil der Merit-Order-Analyse: schreibt capacity_de.csv, energy_hourly_400d.csv und
# fuel_prices.csv (Trenner ;) in den Zielordner. Aufruf im Stack-Verzeichnis (/opt/predictiondb):
#   docs/analysen/merit-order/db_export.sh ./meritorder-daten
set -euo pipefail
ziel=${1:?Zielordner}
mkdir -p "$ziel"
q() { docker compose exec -T postgres sh -c 'psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<<"COPY ($1) TO STDOUT WITH (FORMAT csv, HEADER, DELIMITER ';')"; }

# Installierte EE-Leistung je Technologie am neuesten Tag (Summe über die Cluster)
q "SELECT day, technology, clustering_version, round(sum(capacity_mw)::numeric, 1) AS capacity_mw_de,
          count(*) AS clusters
     FROM assets.capacity_daily
    WHERE day = (SELECT max(day) FROM assets.capacity_daily)
    GROUP BY 1, 2, 3 ORDER BY 2" >"$ziel/capacity_de.csv"

# Stundenmittel der besten Quelle: Preis, Last, Residuallast, Erzeugung, letzte 400 Tage
q "SELECT date_trunc('hour', ts) AS ts, code AS series, round(avg(value)::numeric, 2) AS value,
          min(source) AS source, min(unit) AS unit, count(*) AS n_values
     FROM energy.value_best
    WHERE ts >= now() - interval '400 days'
      AND (code IN ('da_price_de_lu', 'load_actual', 'load_da_forecast', 'residual_load')
           OR code LIKE 'gen\_%')
    GROUP BY 1, 2 ORDER BY 2, 1" >"$ziel/energy_hourly_400d.csv"

# Brennstoff- und Wechselkursreihen ab 2020
q "SELECT src.code AS source, s.code AS series, v.ts, v.value, s.unit, s.resolution, v.published
     FROM energy.value v
     JOIN energy.series s USING (series_id)
     JOIN meta.source src ON src.source_id = s.source_id
    WHERE src.code IN ('worldbank', 'eurostat', 'oil_bulletin', 'ecb', 'eia', 'eia_steo')
      AND v.ts >= '2020-01-01'
    ORDER BY 1, 2, 3" >"$ziel/fuel_prices.csv"
