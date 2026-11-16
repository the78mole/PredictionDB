-- Leistung je Cluster: Gewicht der VM gegen letzten Wert des Bestands (tabellen/gewichte.csv).
-- Nur lesend auf der VM, z. B.:
--   docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" --csv' \
--       < docs/analysen/snapshot-ghcr-abgleich/gewichte.sql \
--       > docs/analysen/snapshot-ghcr-abgleich/tabellen/gewichte.csv
WITH gw AS (
    SELECT technology, cluster_no, sum(capacity_mw) AS grid_weight_mw
    FROM assets.grid_weight
    WHERE clustering_version = 'v2026Q4' AND grid = 'gfs_0p25'
    GROUP BY 1, 2
), last AS (
    SELECT DISTINCT ON (technology, cluster_no)
           technology, cluster_no, capacity_mw AS capacity_daily_last_mw, day AS last_day
    FROM assets.capacity_daily
    WHERE clustering_version = 'v2026Q4'
    ORDER BY technology, cluster_no, day DESC
)
SELECT technology, cluster_no,
       round(grid_weight_mw::numeric, 3) AS grid_weight_mw,
       round(capacity_daily_last_mw::numeric, 3) AS capacity_daily_last_mw,
       last_day
FROM gw FULL JOIN last USING (technology, cluster_no)
ORDER BY technology, cluster_no;
