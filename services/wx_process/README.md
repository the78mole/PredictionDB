# wx-process

Bronze → Silber → Gold für GFS 0,25°: dekodiert die GRIB2-Schritte eines vollständigen Laufs
aus `wx-bronze`, legt sie als Zarr v3 in `wx-silver` ab und rechnet daraus Wetter-Features je
MaStR-Cluster nach `feat.nwp_value`. Grundlage: `docs/ARCHITEKTUR.md` Kap. 5.1 und 7.5;
Konfiguration in `config/process/gfs_0p25.yaml`, Zustand in `meta.nwp_feature_run`
(Alembic-Revision `0003_wx_features`).

## Ablauf je Lauf

1. **Fällig**, sobald `run.json` in Bronze liegt (alle Schritte gespeichert). Gerechnet ist ein
   Lauf, wenn `meta.nwp_feature_run` einen Eintrag für die jüngste Clusterung (mit Gewichten
   auf `weight_grid`) und die aktuelle Feature-Version hat. Eine neue Clusterung aus
   `mastr-ingest` oder eine neue Feature-Version macht die Läufe im Rückblickfenster also
   erneut fällig; ältere Läufe rechnet `backfill` nach.
2. **Dekodieren.** Nachrichten werden über die numerischen GRIB2-Schlüssel erkannt, unbekannte
   (z. B. die Zusatzfelder des NOMADS-Filters) übersprungen. GFS liefert Mittel und Summen in
   Eimern, die alle 6 h neu beginnen (`42-43 ave`, `42-44 ave` … `42-48 ave`); daraus wird der
   Wert für das Intervall seit dem vorherigen Schritt zurückgerechnet. Strahlung ist danach das
   Mittel der letzten Stunde (ab 120 h der letzten drei Stunden), Niederschlag eine Rate in
   mm/h. Im Analyse-Schritt f000 fehlen diese Größen.
3. **Silber** `gfs/0p25/JJJJ/MM/TT/HH.zarr`: je Variable ein Int16-Array `(step, lat, lon)` mit
   `scale_factor`/`add_offset` (CF), Blosc/Zstd mit Byte-Shuffle, Chunks über 48 Schritte und das ganze Gitter. Ein
   Store je Lauf (statt eines wachsenden Stores je Modell), damit Neuberechnung und Aufräumen
   auf einen Lauf beschränkt bleiben. `xarray.open_zarr` liest ihn direkt. Silber wird nur neu
   gebaut, wenn es fehlt, Bronze neuer ist oder sich die Kodierung (`SILVER_VERSION`) ändert.

   | Variable | Einheit | Auflösung | Quelle |
   |---|---|---|---|
   | `t2m`, `d2m` | K | 0,01 | `TMP`, `DPT` 2 m |
   | `r2m` | % | 0,01 | `RH` 2 m |
   | `u10`, `v10`, `u100`, `v100`, `gust` | m/s | 0,01 | `UGRD`/`VGRD` 10 m, 100 m, `GUST` |
   | `dswrf` | W/m² | 0,05 | `DSWRF`, Intervallmittel |
   | `tcc`, `tcc_mean` | % | 0,01 | `TCDC` momentan bzw. Intervallmittel |
   | `tp` | mm/h | 0,001 | `APCP`, Intervallsumme je Stunde |
   | `csnow`, `csnow_mean` | 0–1 | 0,001 | `CSNOW` momentan bzw. Anteil im Intervall |
   | `prmsl` | Pa | 1 | `PRMSL` |
   | `sp` | Pa | 1 | `PRES` am Boden |
   | `dlwrf` | W/m² | 0,05 | `DLWRF`, Intervallmittel |
   | `sunshine` | 0–1 | 0,0001 | `SUNSD`, Sonnenscheinanteil im Intervall (GFS meldet es als Momentanwert, summiert aber in 6-h-Eimern) |
   | `weasd` | kg/m² | 0,1 | `WEASD` |
   | `snod` | m | 0,001 | `SNOD` |
   | `tsoil_0_10`, `tsoil_10_40` | K | 0,01 | `TSOIL` 0–0,1 m, 0,1–0,4 m (über Wasser leer) |
   | `soilw_0_10`, `soilw_10_40` | 0–1 | 0,0001 | `SOILW` 0–0,1 m, 0,1–0,4 m (über Wasser leer) |

   Fehlt eine Variable in Bronze (ältere Läufe vor einer Erweiterung der Quelle), fehlt sie auch
   im Silber-Store dieses Laufs; fehlt sie nur in einzelnen Schritten, bleiben diese leer.

4. **Gold.** Für jedes Cluster aus `assets.grid_weight` (Anteil der Clusterleistung je
   Gitterzelle) werden die Features als gewichtetes Mittel über die Zellen gerechnet.
   Nichtlineare Größen entstehen erst je Zelle und werden dann gewichtet. Features stehen in
   `meta.feature` (Quelle `gfs_0p25`, Aggregation `cluster_cap_weighted`), Werte in
   `feat.nwp_value` mit `region_id` des Clusters (`wind_on:v2026Q4:c07`), `run_time` und
   `valid_time`. Danach folgen `meta.nwp_run` (Status `processed`) und
   `NOTIFY features_ready`.

   | Art (YAML) | Technologien | Features |
   |---|---|---|
   | `wind` | `wind_on`, `wind_off` | `wind_speed_10m`, `wind_speed_100m`, `wind_speed_hub`, `gust`, `t2m`, `wind_cf`, `wind_power` |
   | `pv` | `pv_ground`, `pv_roof` | `ghi`, `t2m`, `cloud_cover`, `snow_fraction`, `pv_cf`, `pv_power` |
   | `hydro` | `hydro` | `precip`, `t2m`, `snow_fraction` |
   | `other` | `biomass` | `t2m` |

   - **Wind in Nabenhöhe:** Potenzgesetz mit dem Exponenten aus 10 m und 100 m je Zelle
     (bei Schwachwind 1/7, begrenzt auf 0–0,6). Nabenhöhe = leistungsgewichtetes Mittel der
     Anlagen des Clusters aus dem Snapshot der Gewichte, sonst `hub_height_m` aus der YAML.
   - **`wind_cf`:** generische Leistungskurve (kubisch zwischen Einschalt- und
     Nenngeschwindigkeit, Abschaltung) je Zelle, gewichtet. Ein Index, kein Anlagenmodell.
   - **`pv_cf`:** `PR · G/1000 · (1 + γ·(T_Zelle − 25 °C))`, `T_Zelle = T_Luft + G·(NOCT − 20)/800`,
     ohne Ausrichtung und Neigung.
   - **`*_power`:** Kapazitätsfaktor mal installierte Leistung des Clusters am Tag der
     Gültigkeit (UTC) aus `assets.capacity_daily`; nach dem letzten Tag gilt der letzte Wert.
     Ohne Bestand entfällt das Feature.
   - Liegen Gewichte außerhalb des Modellgitters, wird über den Rest normiert; der Anteil steht
     als `coverage_below_1` in `meta.nwp_feature_run.stats` und im Log.

Gemessen am Lauf 2026-10-20 06Z (209 Schritte, 4 617 Gitterpunkte, 15 Variablen): Bronze
14,6 MB, Silber 13,0 MB, Aufbau von Silber etwa 5 s, Features für 90 Cluster unter 1 s.
Gold bei den Startwerten der Clusterzahl: 505 Reihen, rund 105 000 Werte je Lauf
(etwa 150 Mio. Zeilen im Jahr bei vier Läufen am Tag).

## Aufruf

```bash
# Datenbank, S3 und Logging wie alle Dienste über predictiondb_core (.env, Präfix PDB_)
uv run wx-process gfs latest                       # fällige Läufe der letzten 48 h
uv run wx-process gfs latest --watch 600           # Dienst (Compose: wx-process)
uv run wx-process gfs run 2026102006 --force       # einen Lauf neu rechnen
uv run wx-process gfs run 2026102006 --silver-only # nur Silber, ohne Datenbank
uv run wx-process gfs backfill 2026-10-01T00 2026-10-07T18 --clustering v2026Q4
uv run wx-process gfs backfill 2021-03-22T12 --runs 0,12,6,18 --newest-first --decompress \
    --watch 3600                                   # Dienst wx-process-backfill
```

Im Backfill gibt `--runs` die Laufzeiten in Reihenfolge der Priorität an (erst alle 00Z, dann
12Z …); Läufe ohne Silber werden übersprungen. `--decompress` entpackt die schon komprimierten
Chunks von `feat.nwp_value`, solange Läufe hineinschreiben, und packt sie danach wieder (ein
`COPY` in komprimierte Chunks ist etwa dreimal langsamer). Auf die Sperre eines Chunks wird
höchstens 5 s gewartet; ein gesperrter Chunk bleibt, wie er ist, und wird nach 10 min erneut
versucht. Nach einem Abbruch packt die Kompressions-Policy die Chunks später.

| Variable | Standard |
|---|---|
| `PDB_DATABASE_URL`, `PDB_S3_*` | siehe `.env.example` |
| `PDB_BUCKET_BRONZE`, `PDB_BUCKET_SILVER` | `wx-bronze`, `wx-silver` |
| `PDB_WX_PROCESS_CONFIG` | `config/process/gfs_0p25.yaml` |

Silber lesen, z. B. im Notebook:

```python
import xarray as xr
ds = xr.open_zarr("s3://wx-silver/gfs/0p25/2026/10/20/06.zarr", storage_options={...})
```

## Tests

```bash
uv run pytest services/wx_process                  # Dekodierung, Silber, Features, Ablauf
PDB_INTEGRATION=1 uv run pytest tests/integration/test_wx_process.py   # gegen Postgres
```

Die Testdaten (`wx_process.testing`) sind synthetische GFS-Schritte mit denselben
GRIB2-Schlüsseln und Eimern wie die echten Dateien; geprüft wird unter anderem, dass die
Rückrechnung wieder die stündlichen Ausgangswerte ergibt.

## Offen

- Bevölkerungsgewichtete Temperatur für die Last (braucht Regionsmasken, `config/regions`).
- Features aus den zusätzlichen Feldern: Luftdichte (`sp`, `t2m`) für Wind, Schneedecke
  (`weasd`, `snod`) für PV, Boden für Wasserkraft und Wärme. Silber führt sie bereits.
- Sonnenstand bzw. Clear-Sky-Normierung der Strahlung (mit `pvlib`, 7.6).
- Weitere Modelle (ECMWF, ICON) über eigene Prozess-YAMLs und Gewichte je Gitter.
