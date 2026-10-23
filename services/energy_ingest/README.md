# energy-ingest

Lädt Day-Ahead-Preise (DE-LU), Netzlast, Residuallast, Erzeugung je Energieträger, die
Folgetag-Prognosen der ÜNB sowie die Monats- und Jahresmarktwerte nach EEG für Deutschland
aus allen kostenfreien Quellen nach TimescaleDB
(Schema `energy`, Kapitel 3.2, 3.3 und 5.3 in `docs/ARCHITEKTUR.md`).

| Quelle | Zugang | Datensätze | Rang | frei |
|---|---|---|---|---|
| SMARD, Bundesnetzagentur (`smard`) | offen | je Reihe ein Datensatz (`da_price_de_lu`, `load_actual`, …) | 10 | ja, CC BY 4.0 |
| Netztransparenz.de, ÜNB (`netztransparenz`) | kostenloser OAuth-Client, `PDB_NETZTRANSPARENZ_CLIENT_ID` und `_CLIENT_SECRET` | `market_value_month`, `market_value_year` | 15 | nein, keine Lizenz veröffentlicht |
| ENTSO-E Transparency (`entsoe`) | kostenloser Token, `PDB_ENTSOE_TOKEN` | `price`, `load`, `load_forecast`, `generation`, `wind_solar_forecast` | 20 | nein, Preis, Last, Erzeugung nicht auf der Freigabeliste |
| Energy-Charts, Fraunhofer ISE (`energy_charts`) | offen | `price`, `public_power`, `forecast_solar`, `forecast_wind_onshore`, `forecast_wind_offshore`, `forecast_load` | 30 | nein, nicht-kommerziell, Börsenpreise nicht weitergebbar |
| Eigene Marktwerte (`pdb_marktwert`) | Berechnung aus `energy.value_best` | `market_value_month`, `market_value_year` | 40 | ja, aus SMARD |

**Nur freie Quellen als Standard.** Unfreie Quellen laufen nur mit ausdrücklicher Freigabe:
`PDB_ALLOW_ENTSO_E`, `PDB_ALLOW_ENERGY_CHARTS`, `PDB_ALLOW_NETZTRANSPARENZ` (`true|false`,
Vorgabe `false`; ohne Präfix `PDB_` ebenfalls gelesen). Beim Start schreibt der Dienst die
Freigaben nach `meta.source.enabled`, und `energy.value_best` liest nur freigegebene Quellen.
Bereits geladene Werte einer gesperrten Quelle bleiben in `energy.value`, fließen aber nicht
mehr in Training, Dashboards und Veröffentlichungen ein. Hintergrund: `docs/ARCHITEKTUR.md`
7.11.1.

Ohne Netztransparenz rechnet `pdb_marktwert` die Marktwerte nach Anlage 1 EEG selbst: `*_base`
als Mittel des Day-Ahead-Preises, `*_solar` und `*_wind_*` mit der Ist-Erzeugung gewichtet, auf
Stundenbasis aus SMARD. Die ÜNB gewichten mit ihrer Online-Hochrechnung und seit Oktober 2025
mit Viertelstundenpreisen, die eigenen Werte sind deshalb Schätzungen. Eine Periode wird erst
nach ihrem Ende und mit mindestens 98 % der Stunden geschrieben. Ist Netztransparenz
freigegeben, entfällt die eigene Rechnung, und die amtlichen Werte haben Vorrang.

Zugang zu den unfreien Quellen, falls freigegeben: Netztransparenz-Client im
[API-Portal](https://api-portal.netztransparenz.de/) anlegen (Quelle der Wahrheit: OpenBao
`it-infra`, `secret/netztransparenz`, Felder `api-client-id` und `api-client-token`).
ENTSO-E-Token: Konto auf [transparency.entsoe.eu](https://transparency.entsoe.eu) anlegen, dann
Mail an `transparency@entsoe.eu` mit Betreff „Restful API access“ und der Konto-Adresse.

## Datenmodell

- Jede Quelle schreibt eigene Reihen (`energy.series`, eindeutig je Quelle und Code), gleiche
  Codes sind über Quellen hinweg vergleichbar: Zeitstempel in UTC am Intervallbeginn,
  Leistungen in **MW** (SMARD liefert MWh je Intervall und wird umgerechnet), Preise in
  **EUR/MWh**.
- `energy.value_best` liefert je Code und Zeitpunkt den Wert der freigegebenen Quelle
  (`meta.source.enabled`) mit dem kleinsten Rang (`meta.source.priority`).
- `published` ist der Zeitpunkt, ab dem ein Wert bekannt war (Leakage-Schutz, Kapitel 7.4):
  der frühere aus Abrufzeit und regulärem Veröffentlichungstermin (Preis: Vortag 13:00,
  Lastprognose: Vortag 10:00, Wind-/PV-Prognose: Vortag 18:00 Ortszeit, Ist-Werte:
  Intervallende + 1 h). Beim Überschreiben bleibt der früheste Zeitpunkt erhalten.
- Prognosen der ÜNB werden je Zeitpunkt überschrieben, es bleibt also die zuletzt
  veröffentlichte Folgetag-Prognose.

- Marktwerte (`mw_*` je Monat, `jw_*` je Kalenderjahr; `*_base`, `*_solar`,
  `*_wind_onshore`, `*_wind_offshore`) stehen am Periodenbeginn (00:00 Ortszeit) in
  EUR/MWh; Netztransparenz liefert ct/kWh. Als Veröffentlichung gilt Periodenende plus zwei
  Wochen. Der laufende Abgleich schaut für sie 95 bzw. 400 Tage zurück, damit nachgereichte
  Werte ankommen. Monatswerte gibt es ab 1/2012, Jahreswerte ab 2013; vor 2015 nur per
  `energy-ingest --source netztransparenz backfill 2011-12-01 2015-01-01`.

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

Einstellungen (Umgebung bzw. `.env`): `PDB_ALLOW_ENTSO_E`, `PDB_ALLOW_ENERGY_CHARTS`,
`PDB_ALLOW_NETZTRANSPARENZ`, `PDB_ENTSOE_TOKEN`, `PDB_NETZTRANSPARENZ_CLIENT_ID`,
`PDB_NETZTRANSPARENZ_CLIENT_SECRET`, `PDB_ENERGY_SOURCES`
(JSON-Liste), `PDB_ENERGY_LOOKBACK_DAYS`, `PDB_ENERGY_HORIZON_DAYS`,
`PDB_ENERGY_BACKFILL_FROM`, `PDB_ENERGY_BACKFILL_CHUNKS_PER_CYCLE`,
`PDB_ENERGY_CHECK_REL_THRESHOLD`.

## Lizenzen der Daten

SMARD: CC BY 4.0, Bundesnetzagentur | SMARD.de. Energy-Charts: nicht-kommerziell mit
Quellenangabe, Börsenpreise von EPEX SPOT/EEX lizenziert und nicht weitergebbar. ENTSO-E:
Nutzungsbedingungen der Transparency Platform, frei (CC BY 4.0) nur die „List of Data available
for free re-use“. Netztransparenz: keine Lizenz veröffentlicht. Die Vermerke stehen in
`meta.source.license`, die Einordnung in `meta.source.free`.
