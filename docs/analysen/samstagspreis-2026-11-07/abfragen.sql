-- Nur lesende Abfragen gegen die Datenbank auf der VM, Ergebnis in daten/vm_export.txt.
-- docker compose exec -T postgres sh -c 'psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < abfragen.sql
\pset pager off
\pset footer off
\pset format unaligned
\pset fieldsep ';'
SET TIME ZONE 'Europe/Berlin';
\echo === 1 Modellversionen
SELECT version, config, variant, role, train_to FROM meta.model_version WHERE role <> 'retired' ORDER BY 1;
\echo === 2 Preisprognose Liefertag 07.11.2026, jüngste Ausgabe je Version vor Gebotsschluss (06.11. 12 Uhr)
WITH iss AS (
  SELECT model_version, max(issue_time) it FROM fcst.prediction
   WHERE target='da_price_de_lu' AND valid_time >= '2026-11-07 00:00' AND valid_time < '2026-11-08 00:00'
     AND issue_time <= '2026-11-06 12:00' GROUP BY 1)
SELECT f.model_version, iss.it issue_time, to_char(f.valid_time,'HH24') h,
       round(avg(f.value) FILTER (WHERE quantile=0.1)::numeric,1) p10,
       round(avg(f.value) FILTER (WHERE quantile=0.5)::numeric,1) p50,
       round(avg(f.value) FILTER (WHERE quantile=0.9)::numeric,1) p90
  FROM fcst.prediction f JOIN iss ON iss.model_version=f.model_version AND f.issue_time=iss.it
 WHERE f.target='da_price_de_lu' AND f.valid_time >= '2026-11-07 00:00' AND f.valid_time < '2026-11-08 00:00'
 GROUP BY 1,2,3 ORDER BY 1,3;
\echo === 3 Tagesmittel P50 je Version und Liefertag 31.10.-07.11. (jüngste Ausgabe vor 12 Uhr am Vortag)
WITH t AS (SELECT generate_series('2026-10-31'::date, '2026-11-07'::date, '1 day')::date d),
iss AS (
  SELECT t.d, p.model_version, max(p.issue_time) it FROM t JOIN fcst.prediction p
    ON p.target='da_price_de_lu' AND p.valid_time >= t.d AND p.valid_time < t.d + 1 AND p.issue_time <= (t.d - 1) + time '12:00'
  GROUP BY 1,2)
SELECT iss.d, iss.model_version, round(avg(p.value)::numeric,1) p50
  FROM iss JOIN fcst.prediction p ON p.model_version=iss.model_version AND p.issue_time=iss.it
   AND p.target='da_price_de_lu' AND p.quantile=0.5 AND p.valid_time >= iss.d AND p.valid_time < iss.d + 1
 GROUP BY 1,2 ORDER BY 2,1;
\echo === 4 Brennstoffe und Gasspeicher: Werte seit 01.08.2026
SELECT code, ts::date, round(value::numeric,3) v FROM energy.value_best
 WHERE (code IN ('gas_eu_m','coal_au_m','coal_za_m','oil_brent_m','fx_usd_eur','retail_de_heating_oil_net','oil_ulsd_nyh')
        OR code LIKE 'gas_storage_de%')
   AND ts >= '2026-08-01' ORDER BY 1,2;
