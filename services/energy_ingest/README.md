# energy-ingest

Lädt Day-Ahead-Preise (DE-LU), Netzlast, Residuallast, Erzeugung je Energieträger, die
Folgetag-Prognosen der ÜNB sowie die Monats- und Jahresmarktwerte nach EEG für Deutschland
aus allen kostenfreien Quellen nach TimescaleDB
(Schema `energy`, Kapitel 3.2, 3.3 und 5.3 in `docs/ARCHITEKTUR.md`). Dazu kommen Preise
fossiler Energieträger und der Euro-Dollar-Kurs (Abschnitt „Brennstoffpreise“).

| Quelle | Zugang | Datensätze | Rang | frei |
|---|---|---|---|---|
| SMARD, Bundesnetzagentur (`smard`) | offen | je Reihe ein Datensatz (`da_price_de_lu`, `load_actual`, …) | 10 | ja, CC BY 4.0 |
| Netztransparenz.de, ÜNB (`netztransparenz`) | kostenloser OAuth-Client, `PDB_NETZTRANSPARENZ_CLIENT_ID` und `_CLIENT_SECRET` | `market_value_month`, `market_value_year` | 15 | nein, keine Lizenz veröffentlicht |
| ENTSO-E Transparency (`entsoe`) | kostenloser Token, `PDB_ENTSOE_TOKEN` | `price`, `load`, `generation`, `wind_solar_forecast` | 20 | nein, Preis, Last, Erzeugung nicht auf der Freigabeliste |
| ENTSO-E, freier Teil (`entsoe_last`) | wie `entsoe`, ohne Schalter | `load_forecast` (Day-Ahead-Lastprognose 6.1.b) | 20 | ja, CC BY 4.0 (Freigabeliste) |
| Energy-Charts, Fraunhofer ISE (`energy_charts`) | offen | `price`, `price_<zone>` (Nachbarzonen AT, BE, CH, CZ, DK1, DK2, FR, HU, IT-North, NL, NO2, PL, SE4, SI als `da_price_<zone>`), `public_power`, `forecast_solar`, `forecast_wind_onshore`, `forecast_wind_offshore`, `forecast_load` | 30 | ja, CC BY 4.0 laut API-Dokumentation; Preise DE-LU und Nachbarzonen CC BY 4.0 (SMARD), Vermerk wird geprüft |
| Eigene Marktwerte (`pdb_marktwert`) | Berechnung aus `energy.value_best` | `market_value_month`, `market_value_year` | 40 | ja, aus SMARD |
| EIA, eigene Reihen (`eia`) | `PDB_EIA_API_KEY`, ohne Schlüssel Bulk-Dateien | `prices` (ULSD New York Harbor und Gulf Coast) | 10 | ja, Public Domain |
| EIA, Reihen von Thomson Reuters (`eia_reuters`) | wie `eia` | `prices` (Brent, WTI, Benzin, Heizöl, Kerosin, Propan, Henry Hub) | 10 | nein, urheberrechtlich geschützt |
| EZB (`ecb`) | offen | `fx` (USD je EUR) | 10 | ja, Weiterverwendung mit Quellenangabe |
| Weltbank, Pink Sheet (`worldbank`) | offen, xlsx | `prices` (Monatsmittel Brent, WTI, Dubai, Kohle AU/ZA, Erdgas US/Europa, LNG Japan) | 10 | ja, CC BY 4.0 |
| EU Weekly Oil Bulletin (`oil_bulletin`) | offen, xlsx | `retail_de` (Super E5, Diesel, Heizöl, LPG mit und ohne Steuern) | 10 | ja, CC BY 4.0 |
| Trading Hub Europe (`the`) | offen | `gas_prices` (VTP-Tagesmittel, Ausgleichsenergiepreise ab 10/2021) | 10 | nein, Vervielfältigung nur mit Zustimmung |

**Nur freie Quellen als Standard.** Unfreie Quellen laufen nur mit ausdrücklicher Freigabe:
`PDB_ALLOW_ENTSO_E`, `PDB_ALLOW_NETZTRANSPARENZ`,
`PDB_ALLOW_EIA_REUTERS`, `PDB_ALLOW_THE` (`true|false`,
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

## Brennstoffpreise

Codes und Einheiten (Notierungseinheit der Quelle, keine Umrechnung):

- Tageswerte am Handelstag 00:00 Ortszeit: `oil_ulsd_nyh`, `oil_ulsd_usgc` (USD/gal, frei),
  `oil_brent`, `oil_wti` (USD/bbl), `oil_gasoline_nyh`, `oil_heating_oil_nyh`, `oil_jet_usgc`,
  `lpg_propane_mb` (USD/gal), `gas_henry_hub` (USD/MMBtu), `fx_usd_eur` (USD je EUR).
- THE je Gastag ab 06:00 Ortszeit (EUR/MWh): `gas_the_vtp`, `gas_the_imbalance_pos`
  (Unterdeckung), `gas_the_imbalance_neg`.
- Oil Bulletin je Stichtag (Montag) in EUR/l: `retail_de_euro95`, `retail_de_diesel`,
  `retail_de_heating_oil`, `retail_de_lpg`, netto mit Suffix `_net`.
- Weltbank je Monat (Suffix `_m`): `oil_brent_m`, `oil_wti_m`, `oil_dubai_m` (USD/bbl),
  `coal_au_m`, `coal_za_m` (USD/t), `gas_henry_hub_m`, `gas_eu_m` (≈ TTF), `lng_japan_m`
  (USD/MMBtu).

Als Veröffentlichung gilt bei Tageswerten das Ende des Folgetags, beim Oil Bulletin der
Stichtag plus sieben Tage, bei Monatsmitteln Monatsende plus sieben Tage. Die Historie reicht
bei EIA bis 1986, beim Oil Bulletin bis 2005, bei der Weltbank bis 1960; vor 2015 nur per
`energy-ingest --source worldbank backfill 1960-01-01 2015-01-01` und entsprechend.

Weltbank und Oil Bulletin liefern ganze xlsx-Dateien, die der Dienst zwölf Stunden im
Speicher hält. Ihre Adressen enthalten eine Dokument-ID und sind einstellbar
(`PDB_ENERGY_WORLDBANK_URL`, `PDB_ENERGY_OIL_BULLETIN_URL`); die Weltbank-Adresse wechselt
jedes Jahr. Die Kopplung der Preise (Brent als Leitpreis der Ölkette, TTF der Gaskette) und
die Bewertung weiterer Quellen stehen im Doc „Preise fossiler Energieträger“.

## Formatprüfung und Alarme

Jede Quelle prüft den Aufbau ihrer Antwort (Blätter, Spaltenköpfe, Einheiten, Reihen-IDs).
Weicht er ab, schreibt sie nichts und meldet `status = 'format'` (Log `energy.format_changed`).
Der letzte Ausgang je Quelle und Datensatz steht in `meta.energy_status`, der jüngste Wert je
Reihe in der Sicht `energy.series_freshness`. Grafana (`config/grafana/provisioning/alerting`)
schickt daraus E-Mails:

- **Format geändert:** sofort.
- **Abruf scheitert seit 24 h:** letzter erfolgreicher Abruf älter als 24 h.
- **Reihe veraltet:** jüngster Wert älter als erwartet (Tageswerte 7 Tage, Wochenwerte
  21 Tage, Monatswerte 75 Tage, Jahreswerte 800 Tage, da ihr Zeitstempel der Jahresbeginn
  ist), etwa weil sich eine Download-Adresse geändert hat.

SMTP-Zugang und Empfänger in `.env`: `PDB_SMTP_HOST` (mit Port), `PDB_SMTP_USER`,
`PDB_SMTP_PASSWORD`, optional `PDB_SMTP_FROM` und `PDB_SMTP_STARTTLS`, dazu `PDB_ALERT_EMAIL`.
Ohne `PDB_SMTP_HOST` bleibt der Versand aus; die Alarme sind dann nur in Grafana sichtbar.

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
abgerufen (`--force` erzwingt es). Vom Block, in den die Sieben-Tage-Grenze fällt, lädt der
Backfill einmal am Tag den Teil vor der Grenze (in `meta.energy_fetch` mit `chunk_end` vor dem
Rasterende); so bleibt zwischen dem letzten vollen Block und dem Fenster des laufenden
Abgleichs keine Lücke. Leer geladene Blöcke werden nach 30 Tagen erneut versucht, die eigenen
Marktwerte (`pdb_marktwert`) schon nach einem Tag, sobald die Grunddaten vollständig sind.

Einstellungen (Umgebung bzw. `.env`): `PDB_ALLOW_ENTSO_E`, `PDB_ALLOW_NETZTRANSPARENZ`,
`PDB_ALLOW_EIA_REUTERS`, `PDB_ALLOW_THE`, `PDB_EIA_API_KEY`, `PDB_ENTSOE_TOKEN`, `PDB_NETZTRANSPARENZ_CLIENT_ID`,
`PDB_NETZTRANSPARENZ_CLIENT_SECRET`, `PDB_ENERGY_SOURCES`
(JSON-Liste), `PDB_ENERGY_LOOKBACK_DAYS`, `PDB_ENERGY_HORIZON_DAYS`,
`PDB_ENERGY_BACKFILL_FROM`, `PDB_ENERGY_BACKFILL_CHUNKS_PER_CYCLE`,
`PDB_ENERGY_CHECK_REL_THRESHOLD`.

## Lizenzen der Daten

SMARD: CC BY 4.0, Bundesnetzagentur | SMARD.de. Energy-Charts: CC BY 4.0 mit Quellenangabe
Energy-Charts.info, soweit nicht anders angegeben (API-Dokumentation v2.0); die Preise DE-LU
stammen von SMARD und stehen laut `license_info` unter CC BY 4.0, andere Gebotszonen nicht.
ENTSO-E:
Nutzungsbedingungen der Transparency Platform, frei (CC BY 4.0) nur die „List of Data available
for free re-use“. Netztransparenz: keine Lizenz veröffentlicht. Die Vermerke stehen in
`meta.source.license`, die Einordnung in `meta.source.free`.
