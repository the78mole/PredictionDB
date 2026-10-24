# Datenquellen mit Zugangsschlüssel oder Lizenzfreigabe

Diese Liste sammelt alle Datenquellen, für die ein API-Schlüssel (Registrierung) und/oder eine
Zustimmung des Anbieters zur Nutzung bzw. Weitergabe nötig ist. Sie ist die Arbeitsliste für
Anfragen an die Anbieter. Grundregel (ARCHITEKTUR 7.11.1, mit PR #34): Standardmäßig laufen nur freie
Quellen; jede unfreie Quelle hat einen eigenen Schalter `PDB_ALLOW_*`, Vorgabe und Einstellung
der VM ist `false`.

Geheimnisse stehen nie im Repo. Quelle der Wahrheit ist OpenBao (Namespace `it-infra`, KV v2
unter `secret/`, ARCHITEKTUR 8.3); auf der VM landen die Werte in `/opt/predictiondb/.env`.

Stand: 2026-10-24. Technische Einordnung, keine Rechtsberatung.

## Übersicht

| Quelle | Zweck | Schlüssel | Lizenz | Schalter | Status |
|---|---|---|---|---|---|
| ENTSO-E Transparency Platform | Day-Ahead-Preis, Ist-Last, Lastprognose, Erzeugung, Wind/PV-Prognose (Abgleich zu SMARD) | ja, Token | nur „List of Data available for free re-use“ frei; Preis, Ist-Last, Erzeugung, Wind/PV-Prognose nicht | `PDB_ALLOW_ENTSO_E` | Token angefragt, Lizenz für genutzte Reihen nicht frei |
| Netztransparenz.de (ÜNB) | amtliche Monats- und Jahresmarktwerte (`mw_*`, `jw_*`) | ja, OAuth2-Client | keine Lizenz veröffentlicht | `PDB_ALLOW_NETZTRANSPARENZ` | Zugang vorhanden, Lizenzanfrage offen |
| Energy-Charts (Fraunhofer ISE) | Preis, Erzeugung, PV-Prognose; Wunsch: Terminpreise | nein | nicht-kommerziell, EPEX/EEX-Preise nicht weitergebbar | `PDB_ALLOW_ENERGY_CHARTS` | nicht frei; Anfrage nach Futures-Endpunkt offen |
| EIA, Reihen von Thomson Reuters (`eia_reuters`, PR #38) | Brent, WTI, Benzin, Heizöl, Kerosin, Propan, Henry Hub | ja (kostenlos), auch ohne Schlüssel per Bulk-Datei | urheberrechtlich geschützt (Thomson Reuters) | `PDB_ALLOW_EIA_REUTERS` | Schlüssel vorhanden; Lizenz nicht frei, keine Anfrage geplant |
| EIA, eigene Reihen (`eia`, PR #38) | ULSD-Notierungen (Diesel/Gasöl) | wie oben | Public Domain | – (frei) | Schlüssel vorhanden |
| Trading Hub Europe (`the`, PR #38) | Tagespreis Gas am VTP, Ausgleichsenergiepreise | nein (User-Agent Pflicht) | Vervielfältigung nur mit schriftlicher Zustimmung | `PDB_ALLOW_THE` | Zustimmung offen |
| EEX (CO2-Primärauktionen, Terminpreise Strom) | EUA-Preise, Phelix-DE-Futures | – | Disclaimer: nur einzelne persönliche Kopie; Weitergabe nur mit Zustimmung bzw. Abo | – | scheidet aus, nicht umgesetzt |

## Je Quelle

### ENTSO-E Transparency Platform

- **Code:** `services/energy_ingest/src/energy_ingest/sources/entsoe.py`, Quelle `entsoe`.
- **Schlüssel:** Konto auf <https://transparency.entsoe.eu>, dann E-Mail an
  `transparency@entsoe.eu`, Betreff „Restful API access“.
- **OpenBao:** `secret/predictiondb`, Feld `entsoe_token` (ARCHITEKTUR 8.3).
- **ENV:** `PDB_ENTSOE_TOKEN`; ohne Token bleibt die Quelle aus.
- **Lizenz:** Nutzungsbedingungen vom 29.03.2023, Ziff. 3.1. Frei (CC BY 4.0) ist nur die
  Freigabeliste vom 18.10.2023. Von den genutzten Reihen steht nur die Day-Ahead-Lastprognose
  (6.1.b) darauf; Ist-Last (6.1.a), Day-Ahead-Preis (12.1.d), Erzeugung (16.1) und
  Wind/PV-Prognose (14.1.d) nicht.
- **Schalter:** `PDB_ALLOW_ENTSO_E=false`.
- **Status:** Token angefragt. Eine Lizenzanfrage wäre bei den Primärdateninhabern (ÜNB, EPEX)
  zu stellen; ohne sie bleibt nur die Lastprognose als freie Reihe.

### Netztransparenz.de

- **Code:** `services/energy_ingest/src/energy_ingest/sources/netztransparenz.py`, Quelle
  `netztransparenz` (PR #31).
- **Schlüssel:** Konto im API-Portal <https://api-portal.netztransparenz.de>, dort einen Client
  anlegen (OAuth2 Client Credentials).
- **OpenBao:** `secret/netztransparenz`, Felder `api-client-id` und `api-client-token`.
- **ENV:** `PDB_NETZTRANSPARENZ_CLIENT_ID`, `PDB_NETZTRANSPARENZ_CLIENT_SECRET` (auf der VM
  eingetragen).
- **Lizenz:** weder auf der Website noch in der WebAPI-Doku (v1.14) eine Lizenz gefunden.
- **Schalter:** `PDB_ALLOW_NETZTRANSPARENZ=false`; ersatzweise rechnet `pdb_marktwert` die
  Marktwerte aus SMARD.
- **Status:** Zugang vorhanden und getestet. Offen: Nutzungsbedingungen bei den ÜNB erfragen
  (Voraussetzung für Modelle mit Netztransparenz-Daten, ARCHITEKTUR 7.11.1).

### Energy-Charts (Fraunhofer ISE)

- **Code:** `services/energy_ingest/src/energy_ingest/sources/energy_charts.py`, Quelle
  `energy_charts`.
- **Schlüssel:** keiner (`api.energy-charts.info` ist offen).
- **Lizenz:** Daten frei für nicht-kommerzielle und journalistische Zwecke mit Nennung
  „Energy-Charts / Fraunhofer ISE“; Spot- und Terminpreise sind von EPEX SPOT/EEX lizenziert
  und dürfen nicht weitergegeben werden.
- **Schalter:** `PDB_ALLOW_ENERGY_CHARTS=false`.
- **Status:** nicht frei. Anfrage an Fraunhofer ISE, Terminpreise (Futures) in die API
  aufzunehmen, ist gestellt; Antwort offen. Terminpreise bleiben bis dahin offen.

### EIA (U.S. Energy Information Administration), PR #38

- **Code:** `services/energy_ingest/src/energy_ingest/sources/eia.py`, Quellen `eia` (frei) und
  `eia_reuters` (unfrei).
- **Schlüssel:** kostenlos unter <https://www.eia.gov/opendata/register.php>. Ohne Schlüssel lädt
  die Quelle die Bulk-Dateien `PET.zip` und `NG.zip`.
- **OpenBao:** `secret/eia`; `betrieb/env-aus-openbao.sh` (Projektdateien) überträgt den Wert.
- **ENV:** `PDB_EIA_API_KEY`.
- **Lizenz:** Reihen der EIA selbst (`copyright: None`, ULSD) sind Public Domain. Brent, WTI,
  Benzin, Heizöl, Kerosin, Propan und Henry Hub bezieht die EIA von Thomson Reuters; sie sind
  urheberrechtlich geschützt.
- **Schalter:** `PDB_ALLOW_EIA_REUTERS=false` (nur für `eia_reuters`).
- **Status:** Schlüssel in OpenBao vorhanden; Eintrag in die VM-`.env` macht der Nutzer selbst.
  Eine Freigabe der Reuters-Reihen ist nicht realistisch; Ersatz für Öl/Gas sind Weltbank
  (monatlich) und Oil Bulletin (wöchentlich).

### Trading Hub Europe (THE), PR #38

- **Code:** `services/energy_ingest/src/energy_ingest/sources/the.py`, Quelle `the`.
- **Schlüssel:** keiner; DataExport-API `api.tradinghub.eu/api/dataexport/xmlexport/` verlangt
  einen aussagekräftigen User-Agent.
- **Lizenz:** Disclaimer: Vervielfältigung der Daten nur mit schriftlicher Zustimmung von THE.
- **Schalter:** `PDB_ALLOW_THE=false`.
- **Status:** Zustimmung offen (Anfrage an THE vorgeschlagen, noch nicht gestellt).

### EEX

- **Zweck:** CO2-Preise (EUA-Primärauktionen, xlsx ab 2012) und Terminpreise Strom.
- **Lizenz:** Disclaimer erlaubt nur eine einzelne persönliche Kopie; maschinelle oder
  regelmäßige Weitergabe nur mit Zustimmung (`datasource@eex-group.com`) bzw. kostenpflichtigem
  Abo (EEX DataSource).
- **Status:** scheidet aus, nicht umgesetzt.

## Freie Quellen ohne Schlüssel (zur Abgrenzung)

Diese Quellen brauchen weder Registrierung noch Zustimmung, nur den Quellenvermerk:
NOAA GFS/GEFS (gemeinfrei), DWD Open Data, ECMWF Open Data (CC BY 4.0), MaStR (DL-DE BY 2.0),
SMARD (CC BY 4.0), `pdb_marktwert` (eigene Berechnung aus SMARD) sowie mit PR #38 EZB
(Weiterverwendung mit Quellenangabe), Weltbank Pink Sheet und EU Weekly Oil Bulletin
(CC BY 4.0).

## Offene Anfragen

1. ENTSO-E: Token (angefragt).
2. Netztransparenz: Nutzungsbedingungen der Daten bei den ÜNB erfragen.
3. Energy-Charts: Futures-Endpunkt in der API (angefragt).
4. THE: schriftliche Zustimmung zur Nutzung der VTP-Preise.

Wird eine Freigabe erteilt: hier den Status ändern, ARCHITEKTUR 7.11.1 nachziehen und den
Schalter in der VM-`.env` auf `true` setzen.
