-- MaStR-Leistung je 0,5°-Zelle für config/gefs/regions_v<n>.json.gz (wx-exchange gefs regions --mastr).
-- Auf der VM: psql -At … -c "\copy (<diese Abfrage>) TO 'de_capacity_0p5.csv' WITH CSV HEADER"
WITH s AS (SELECT max(snapshot_id) AS id FROM assets.snapshot WHERE status = 'imported')
SELECT
    u.technology,
    floor(u.lat * 2 + 0.5) / 2 AS lat,
    floor(u.lon * 2 + 0.5) / 2 AS lon,
    round((sum(u.capacity_kw) / 1000.0)::numeric, 3) AS mw,
    round((
        sum(u.capacity_kw * u.hub_height_m) FILTER (WHERE u.hub_height_m > 0)
        / nullif(sum(u.capacity_kw) FILTER (WHERE u.hub_height_m > 0), 0)
    )::numeric, 1) AS hub_m,
    count(*) AS units,
    (SELECT export_date FROM assets.snapshot WHERE snapshot_id = s.id) AS export_date
FROM assets.unit u, s
WHERE u.snapshot_id = s.id
  AND u.status = 'active'
  AND u.technology IN ('wind_on', 'wind_off', 'pv_ground', 'pv_roof')
  AND u.lat IS NOT NULL AND u.lon IS NOT NULL
GROUP BY 1, 2, 3, s.id
ORDER BY 1, 2, 3
