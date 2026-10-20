# energy-ingest

Lädt Day-Ahead-Preise (DE-LU), Netzlast, Residuallast, Erzeugung je Energieträger und die
Folgetag-Prognosen der ÜNB für Deutschland aus allen kostenfreien Quellen nach TimescaleDB
(Schema `energy`, Kapitel 3.2, 3.3 und 5.3 in `docs/ARCHITEKTUR.md`).

| Quelle | Zugang | Datensätze | Rang |
|---|---|---|---|
| ENTSO-E Transparency (`entsoe`) | kostenloser Token, `PDB_ENTSOE_TOKEN` | `price`, `load`, `load_forecast`, `generation`, `wind_solar_forecast` | 10 |
| SMARD, Bundesnetzagentur (`smard`) | offen | je Reihe ein Datensatz (`da_price_de_lu`, `load_actual`, …) | 20 |
| Energy-Charts, Fraunhofer ISE (`energy_charts`) | offen | `price`, `public_power`, `forecast_solar`, `forecast_wind_onshore`, `forecast_wind_offshore`, `forecast_load` | 30 |

Ohne Token läuft der Dienst mit SMARD und Energy-Charts; ENTSO-E kommt dazu, sobald der
Token in der `.env` steht. Token beantragen: Konto auf
[transparency.entsoe.eu](https://transparency.entsoe.eu) anlegen, dann Mail an
`transparency@entsoe.eu` mit Betreff „Restful API access“ und der Konto-Adresse.

## Datenmodell

- Jede Quelle schreibt eigene Reihen (`energy.series`, eindeutig je Quelle und Code), gleiche
  Codes sind über Quellen hinweg vergleichbar: Zeitstempel in UTC am Intervallbeginn,
  Leistungen in **MW** (SMARD liefert MWh je Intervall und wird umgerechnet), Preise in
  **EUR/MWh**.
- `energy.value_best` liefert je Code und Zeitpunkt den Wert der Quelle mit dem kleinsten
  Rang (`meta.source.priority`).
- `published` ist der Zeitpunkt, ab dem ein Wert bekannt war (Leakage-Schutz, Kapitel 7.4):
  der frühere aus Abrufzeit und regulärem Veröffentlichungstermin (Preis: Vortag 13:00,
  Lastprognose: Vortag 10:00, Wind-/PV-Prognose: Vortag 18:00 Ortszeit, Ist-Werte:
  Intervallende + 1 h). Beim Überschreiben bleibt der früheste Zeitpunkt erhalten.
- Prognosen der ÜNB werden je Zeitpunkt überschrieben, es bleibt also die zuletzt
  veröffentlichte Folgetag-Prognose.

Wichtige Codes: `da_price_de_lu`, `load_actual`, `load_da_forecast`, `residual_load`,
`gen_<träger>` (z. B. `gen_wind_onshore`, `gen_solar`, `gen_lignite`),
`gen_da_forecast_<träger>`. Die vollständige Liste steht in `energy_ingest/model.py`.

## Betrieb

```sh
energy-ingest sources                          # aktive Quellen und Datensätze
energy-ingest sync                             # Fenster um heute einmal abgleichen
energy-ingest sync --watch 900                 # Dauerbetrieb (Compose-Dienst energy-ingest)
energy-ingest backfill 2015-01-01 2026-10-01   # Historie gezielt nachladen
energy-ingest --source smard backfill 2024-01-01 2024-02-01 --dataset da_price_de_lu
energy-ingest check --days 14                  # Quellen gegeneinander prüfen (Stundenmittel)
```

Im Dauerbetrieb gleicht der Dienst alle 15 min das Fenster von drei Tagen zurück bis zwei
Tage voraus ab, prüft die Quellen gegeneinander (Warnung `energy.sources_disagree` ab 5 %
Abweichung) und lädt danach einige Blöcke Historie nach, neueste zuerst, reihum je Quelle,
bis `PDB_ENERGY_BACKFILL_FROM` (Vorgabe 2015-01-01) erreicht ist. Vollständig geladene,
mindestens sieben Tage alte Blöcke stehen in `meta.energy_fetch` und werden nicht erneut
abgerufen (`--force` erzwingt es).

Einstellungen (Umgebung bzw. `.env`): `PDB_ENTSOE_TOKEN`, `PDB_ENERGY_SOURCES`
(JSON-Liste), `PDB_ENERGY_LOOKBACK_DAYS`, `PDB_ENERGY_HORIZON_DAYS`,
`PDB_ENERGY_BACKFILL_FROM`, `PDB_ENERGY_BACKFILL_CHUNKS_PER_CYCLE`,
`PDB_ENERGY_CHECK_REL_THRESHOLD`.

## Lizenzen der Daten

SMARD: CC BY 4.0, Bundesnetzagentur | SMARD.de. Energy-Charts: CC BY 4.0, Fraunhofer ISE.
ENTSO-E: Nutzungsbedingungen der Transparency Platform. Die Vermerke stehen in
`meta.source.license`.
