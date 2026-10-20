# mastr-ingest

Quartalsweiser Import des Marktstammdatenregisters (MaStR, Gesamtdatenexport, Lizenz
DL-DE BY 2.0), Rekonstruktion des Anlagenbestands je Tag und Gebietscluster je Technologie
passend zum GFS-Gitter. Grundlage: `docs/ARCHITEKTUR.md` Kap. 7.5; Konfiguration in
`config/sources/mastr.yaml`, Schema `assets` (Alembic-Revision `0002_assets`).

## Ablauf eines Snapshots

1. **Export sichern.** Link auf der Datendownload-Seite suchen, sonst über das Namensschema
   `Gesamtdatenexport_JJJJMMTT_<Release>.zip` (Release wechselt am 1. April und 1. Oktober).
   Der Export eines Tages liegt nur an diesem Tag vor. Das ZIP (einige GB) wird mit Fortsetzen
   geladen und unverändert nach `assets-bronze/mastr/JJJJ-MM-TT/` geschrieben, danach
   `manifest.json` (URL, Release, SHA-256, Größe).
2. **Einheiten importieren.** `EinheitenSolar_*.xml`, `EinheitenWind.xml`,
   `EinheitenBiomasse.xml`, `EinheitenWasser.xml` werden mit `lxml.iterparse` gestreamt
   (UTF-16) und per `COPY` nach `assets.unit` geschrieben. Aufzählungen werden über
   `Katalogwerte.xml` als Text ausgewertet:

   | Technologie | Regel |
   |---|---|
   | `wind_on` / `wind_off` | `WindAnLandOderAufSee` („auf See“) bzw. Bundesland „Ausschließliche Wirtschaftszone“ |
   | `pv_ground` | `ArtDerSolaranlage` Freifläche oder Sonstige (ohne Angabe: ab 1 MW) |
   | `pv_roof` | Gebäude oder steckerfertig (Balkonkraftwerk) |
   | `biomass`, `hydro` | Einheitentabelle |

   Status: endgültig stillgelegt ohne Datum gilt ab der letzten Aktualisierung als stillgelegt,
   vorübergehend stillgelegt ab Beginn der Stilllegung (oder letzter Aktualisierung).
3. **Verorten** (`geo_precision`): eigene Koordinaten (vertauschte werden korrigiert) →
   Seegebiet für Offshore ohne Koordinaten → Gemeinde (`assets.gemeinde`, sonst Mittel der
   Einheiten mit Koordinaten derselben Gemeinde) → PLZ → Kreis → Bundesland. Kleine Anlagen
   (bis 30 kW, vor allem Dach-PV) haben im öffentlichen Export keine Koordinaten.
4. **Kennzahlen** je Technologie (Anzahl, aktive Leistung, Leistung je Verortungsstufe) und
   Unterschied zum Vorgänger (neue, entfallene, nachgemeldete Einheiten) landen in
   `assets.snapshot.stats` und im Log. Einheiten älterer Snapshots werden nach
   `keep_snapshots` gelöscht, das ZIP bleibt.
5. **Cluster.** Leistung je Zelle des 0,25°-Gitters summieren, leistungsgewichtetes k-Means je
   Technologie (Zahl je Technologie in der YAML). Cluster 0 ist das mit der meisten Leistung;
   Region-Codes `wind_on:v2026Q4:c07` in `meta.region`. Eine Clusterung bleibt
   `recluster_after_days` (365) lang bestehen, neue Zellen kommen zum nächstgelegenen Zentrum.
   Ein älterer Export (Nachimport) ändert die Clusterung nicht.
6. **Gewichte und Bestand.** `assets.grid_weight`: Leistung und Anteil je Gitterzelle und
   Cluster für jedes Gitter aus `weight_grids` (Anteile je Cluster summieren sich auf 1).
   `assets.capacity_daily`: installierte Leistung je Cluster und Tag ab `history_start` bis zum
   Exportdatum, aus Inbetriebnahme, Stilllegung und Wiederaufnahme. Für spätere Tage gilt der
   letzte Wert.

Leistungsindex eines Clusters für eine Prognose (in `wx-process`):
`Σ_Zelle share × Wettergröße(Zelle) × capacity_daily(Tag)`.

## Aufruf

```bash
# Datenbank, S3 und Logging wie alle Dienste über predictiondb_core (.env, Präfix PDB_)
uv run mastr-ingest snapshot                         # heutigen Export laden und verarbeiten
uv run mastr-ingest snapshot --if-due --watch 21600  # Dienst: einmal je Quartal
uv run mastr-ingest import ~/Downloads/Gesamtdatenexport_20261001_26.1.zip
uv run mastr-ingest cluster --new                    # neue Clusterversion, jüngster Snapshot
uv run mastr-ingest gemeinden schwerpunkte.csv       # Spalten ags,name,lat,lon (z. B. aus VG250)
```

| Variable | Standard |
|---|---|
| `PDB_DATABASE_URL`, `PDB_S3_*` | siehe `.env.example` |
| `PDB_BUCKET_ASSETS_BRONZE` | `assets-bronze` |
| `PDB_MASTR_CONFIG` | `config/sources/mastr.yaml` |
| `PDB_MASTR_WORK_DIR` | `$TMPDIR/mastr` (braucht Platz für das ZIP, wird nach dem Import geleert) |

Laufzeit bei rund 5,5 Mio. Einheiten (Messung mit synthetischen Daten hochgerechnet): Parsen und
`COPY` etwa 5–6 min, Verortung und Cluster wenige Minuten.

## Tests

```bash
uv run pytest services/mastr_ingest              # Parser, Download, Cluster, Bestand (offline)
PDB_INTEGRATION=1 uv run pytest tests/integration/test_mastr.py   # gegen Postgres nach migrate
```
