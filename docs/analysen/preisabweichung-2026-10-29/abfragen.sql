-- Abfragen gegen die Datenbank (nur lesend), Ergebnis in daten/. Teil 1 und 2 liefen am 28.10.2026 ~14:30 UTC.
-- psql -U $POSTGRES_USER -d $POSTGRES_DB < abfragen.sql

-- Teil 1
\pset pager off
\pset footer off
SET TIME ZONE 'Europe/Berlin';
\echo === 1 Aktive Versionen
SELECT * FROM meta.model_version ORDER BY 1 DESC LIMIT 12;
\echo === 2 Ausgabezeitpunkte fuer Liefertag 29.10.
SELECT model_version, issue_time, count(*) FROM fcst.prediction
 WHERE target='da_price_de_lu' AND valid_time >= '2026-10-29 00:00' AND valid_time < '2026-10-30 00:00'
 GROUP BY 1,2 ORDER BY 1,2;
\echo === 3 Preis je Stunde: Ist (Stundenmittel) und P10/P50/P90 je Version, juengste Ausgabe vor 12 Uhr am 28.10.
WITH ist AS (
  SELECT date_trunc('hour', ts) h, avg(value) ist, string_agg(DISTINCT source, ',') src
    FROM energy.value_best WHERE code='da_price_de_lu' AND ts >= '2026-10-29 00:00' AND ts < '2026-10-30 00:00' GROUP BY 1),
iss AS (
  SELECT model_version, max(issue_time) it FROM fcst.prediction
   WHERE target='da_price_de_lu' AND valid_time >= '2026-10-29 00:00' AND valid_time < '2026-10-30 00:00'
     AND issue_time <= '2026-10-28 12:00' GROUP BY 1),
p AS (
  SELECT f.model_version, f.valid_time h,
         max(value) FILTER (WHERE quantile=0.1) p10, max(value) FILTER (WHERE quantile=0.5) p50, max(value) FILTER (WHERE quantile=0.9) p90
    FROM fcst.prediction f JOIN iss ON iss.model_version=f.model_version AND f.issue_time=iss.it
   WHERE target='da_price_de_lu' AND valid_time >= '2026-10-29 00:00' AND valid_time < '2026-10-30 00:00' GROUP BY 1,2)
SELECT p.model_version, to_char(p.h,'HH24') h, round(ist.ist::numeric,1) ist, round(p10::numeric,1) p10, round(p50::numeric,1) p50, round(p90::numeric,1) p90,
       round((ist.ist-p50)::numeric,1) diff, ist.src
  FROM p LEFT JOIN ist USING (h) ORDER BY 1,2;
\echo === 3b Ausgabezeit je Version
SELECT model_version, max(issue_time) FROM fcst.prediction WHERE target='da_price_de_lu' AND valid_time >= '2026-10-29' AND valid_time < '2026-10-30' AND issue_time <= '2026-10-28 12:00' GROUP BY 1;
\echo === 4 Stufe 1 und weitere Ziele: P50 (juengste Ausgabe <=12 Uhr) vs. UeNB-Prognose, Stundenmittel 29.10.
WITH iss AS (
  SELECT model_version, target, max(issue_time) it FROM fcst.prediction
   WHERE valid_time >= '2026-10-29 00:00' AND valid_time < '2026-10-30 00:00' AND issue_time <= '2026-10-28 12:00' GROUP BY 1,2)
SELECT f.model_version, f.target, to_char(f.valid_time,'HH24') h, round(f.value::numeric,3) p50
  FROM fcst.prediction f JOIN iss ON iss.model_version=f.model_version AND iss.target=f.target AND f.issue_time=iss.it
 WHERE f.quantile=0.5 AND f.target <> 'da_price_de_lu' AND f.valid_time >= '2026-10-29 00:00' AND f.valid_time < '2026-10-30 00:00'
 ORDER BY 2,1,3;
\echo === 5 UeNB-Prognosen und Bestand 29.10. je Stunde
SELECT code, to_char(date_trunc('hour',ts),'HH24') h, round(avg(value)::numeric,1) v, min(published) pub, string_agg(DISTINCT source, ',') src
  FROM energy.value_best WHERE code IN ('load_da_forecast','gen_da_forecast_wind_onshore','gen_da_forecast_wind_offshore','gen_da_forecast_solar','cap_wind_on','cap_wind_off','cap_pv')
   AND ts >= '2026-10-29 00:00' AND ts < '2026-10-30 00:00' GROUP BY 1,2 ORDER BY 1,2;
\echo === 6 Tagesmittel Preis, Last, Wind, PV, Residuallast letzte 40 Tage
SELECT date_trunc('day', ts)::date d, code, round(avg(value)::numeric,1) v
  FROM energy.value_best WHERE code IN ('da_price_de_lu','load_actual','gen_wind_onshore','gen_wind_offshore','gen_solar','residual_load')
   AND ts >= '2026-09-18' AND ts < '2026-10-30' GROUP BY 1,2 ORDER BY 2,1;
\echo === 7 Brennstoffe, Marktwerte, STEO, Gas: juengste Werte
SELECT code, max(ts) last_ts, (array_agg(value ORDER BY ts DESC))[1] last_v, (array_agg(value ORDER BY ts DESC))[2] prev_v,
       (array_agg(value ORDER BY ts DESC))[5] v5, max(published) pub, string_agg(DISTINCT source, ',') src
  FROM energy.value_best WHERE (code IN ('fx_usd_eur','oil_ulsd_nyh','oil_ulsd_usgc','retail_de_diesel_net','retail_de_heating_oil_net','gas_eu_m','lng_japan_m','gas_henry_hub_m','coal_au_m','coal_za_m','oil_brent_m','gas_the_vtp','mw_base','jw_base','mw_wind_onshore','mw_solar')
   OR code LIKE 'steo_brent_h0%' OR code LIKE 'gas_storage_de%')
   AND ts >= '2025-06-01' GROUP BY 1 ORDER BY 1;
\echo === 7b Zeitreihe Brennstoffe seit August (Tag/Woche/Monat)
SELECT code, ts::date, round(value::numeric,3) v FROM energy.value_best
 WHERE code IN ('oil_ulsd_nyh','fx_usd_eur','retail_de_heating_oil_net','gas_eu_m','coal_au_m','oil_brent_m','gas_storage_de')
   AND ts >= '2026-08-01' ORDER BY 1,2;
\echo === 8 Nachbarpreise Tagesmittel 22.-29.10.
SELECT code, date_trunc('day',ts)::date d, round(avg(value)::numeric,1) v FROM energy.value_best
 WHERE code IN ('da_price_fr','da_price_nl','da_price_at','da_price_pl','da_price_dk1','da_price_ch','da_price_it_north','da_price_cz')
   AND ts >= '2026-10-22' AND ts < '2026-10-30' GROUP BY 1,2 ORDER BY 1,2;
\echo === 9 Stundenpreise 29.10. vs 28.10. vs 22.10.
SELECT to_char(date_trunc('hour',ts),'HH24') h, date_trunc('day',ts)::date d, round(avg(value)::numeric,1) v FROM energy.value_best
 WHERE code='da_price_de_lu' AND (ts::date IN ('2026-10-29','2026-10-28','2026-10-22','2026-10-27')) GROUP BY 1,2 ORDER BY 2,1;
\echo === 10 Fehler der letzten 10 Liefertage (D+1, Ausgabe 08 UTC) je Version
SELECT f.model_version, date_trunc('day',f.valid_time)::date d, round(avg(abs(b.value - f.value))::numeric,1) mae, round(avg(b.value - f.value)::numeric,1) bias, count(*)
  FROM fcst.prediction f
  JOIN LATERAL (SELECT avg(value) value FROM energy.value_best b WHERE b.code='da_price_de_lu' AND b.ts >= f.valid_time AND b.ts < f.valid_time + interval '1 hour') b ON true
 WHERE f.target='da_price_de_lu' AND f.quantile=0.5 AND f.valid_time >= '2026-10-19' AND f.valid_time < '2026-10-30'
   AND extract(hour from f.issue_time AT TIME ZONE 'UTC') = 8 AND f.valid_time::date = (f.issue_time::date + 1)
 GROUP BY 1,2 ORDER BY 1,2;

-- Teil 2
\pset pager off
\pset footer off
SET TIME ZONE 'Europe/Berlin';
\echo === A Baender P10/P50/P90 Ausgabe 28.10. 09 Uhr (08 UTC)
SELECT model_version, to_char(valid_time,'HH24') h, round(quantile::numeric,1) q, round(value::numeric,1) v
  FROM fcst.prediction WHERE target='da_price_de_lu' AND issue_time='2026-10-28 08:00+00'
   AND valid_time >= '2026-10-29 00:00' AND valid_time < '2026-10-30 00:00' ORDER BY 1,2,3;
\echo === B Frische UeNB-Prognosen
SELECT code, max(ts) last_ts, max(published) pub, string_agg(DISTINCT source, ',') src FROM energy.value_best
 WHERE code LIKE '%da_forecast%' AND ts >= '2026-10-20' GROUP BY 1 ORDER BY 1;
\echo === C Stage-1-Prognosen Ausgabe 08 UTC fuer 28.10. vs Ist (Tagesmittel)
SELECT f.model_version, f.target, round(avg(f.value)::numeric) p50, round(avg(b.value)::numeric) ist
  FROM fcst.prediction f
  JOIN LATERAL (SELECT avg(value) value FROM energy.value_best b WHERE b.code=f.target AND b.ts >= f.valid_time AND b.ts < f.valid_time + interval '1 hour') b ON true
 WHERE f.issue_time='2026-10-27 08:00+00' AND f.quantile::numeric=0.5 AND f.valid_time >= '2026-10-28' AND f.valid_time < '2026-10-29'
 GROUP BY 1,2 ORDER BY 2,1;
\echo === D CSV Tageswerte
\copy (SELECT date_trunc('day', ts)::date d, code, avg(value) v FROM energy.value_best WHERE code IN ('da_price_de_lu','residual_load','gen_wind_onshore','gen_wind_offshore','load_actual','da_price_fr','da_price_at','da_price_it_north','da_price_pl') AND ts >= '2018-10-01' GROUP BY 1,2 ORDER BY 2,1) TO STDOUT WITH CSV HEADER
\echo === E CSV Brennstoffe Rohwerte
\copy (SELECT code, ts, value, published, source FROM energy.value_best WHERE code IN ('fx_usd_eur','oil_ulsd_nyh','oil_ulsd_usgc','retail_de_diesel_net','retail_de_heating_oil_net','gas_eu_m','lng_japan_m','gas_henry_hub_m','coal_au_m','coal_za_m','oil_brent_m','gas_storage_de','steo_brent_h00','steo_brent_h01','steo_henry_hub_h00','mw_base') AND ts >= '2018-01-01' ORDER BY 1,2) TO STDOUT WITH CSV HEADER
\echo === F Quellen mit CO2 oder Gas taeglich
SELECT src.code, src.enabled, s.code, count(v.*) n, max(v.ts) FROM energy.series s JOIN meta.source src USING (source_id) LEFT JOIN energy.value v USING (series_id)
 WHERE s.code ~* '(co2|eua|ttf|vtp|gas_the|carbon)' GROUP BY 1,2,3 ORDER BY 1,3;
