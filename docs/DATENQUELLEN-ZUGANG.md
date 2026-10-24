# Datenquellen mit Zugangsschlüssel oder Lizenzfreigabe

Diese Anleitung richtet sich an Betreiber, die den Stack neu aufsetzen. Sie beschreibt alle
Datenquellen, für die ein API-Schlüssel (Registrierung) und/oder eine Zustimmung des Anbieters
nötig ist: wo man sich registriert, welche Variablen in die `.env` gehören und welche
Lizenzfragen der Betreiber selbst klären muss.

Die Einordnung der Bedingungen ist eine technische Einschätzung, keine Rechtsberatung. Jeder
Betreiber ist selbst dafür verantwortlich, die Nutzungsbedingungen für seinen Zweck zu prüfen.

## Grundregel: ohne Zutun nur freie Quellen

Nach dem Aufsetzen laufen nur Quellen, deren Bedingungen eine freie Nutzung und Weitergabe
erlauben. Sie brauchen weder Schlüssel noch Zustimmung (siehe [Freie Quellen](#freie-quellen-ohne-schlüssel)).
Damit liefert der Stack alles, was Training und Prognose brauchen; Marktwerte rechnet die
Quelle `pdb_marktwert` selbst aus SMARD.

Jede unfreie Quelle hat einen eigenen Schalter `PDB_ALLOW_*` mit der Vorgabe `false`. Erst mit
`true` **und**, wo nötig, einem Schlüssel wird sie abgerufen. Daten unfreier Quellen fließen nur in
`energy.value_best` ein, solange der Schalter an ist.

Wer trainierte Modelle oder Daten weitergeben will, lässt alle `PDB_ALLOW_*` auf `false`
(geplant: `pdb-model publish` bricht ab, sobald eine unfreie Quelle im Trainingsdatensatz steckt).

## Übersicht

| Quelle | Wofür | Schlüssel | Variablen | Schalter | Lizenzfrage für den Betreiber |
|---|---|---|---|---|---|
| ENTSO-E Transparency Platform | Day-Ahead-Preis, Ist-Last, Lastprognose, Erzeugung, Wind/PV-Prognose (Abgleich zu SMARD) | ja, Token | `PDB_ENTSOE_TOKEN` | `PDB_ALLOW_ENTSO_E` | Nur die „List of Data available for free re-use“ ist frei; für Preis, Ist-Last, Erzeugung und Wind/PV-Prognose Rechte der Primärdateninhaber beachten |
| Netztransparenz.de (ÜNB) | amtliche Monats- und Jahresmarktwerte | ja, OAuth2-Client | `PDB_NETZTRANSPARENZ_CLIENT_ID`, `PDB_NETZTRANSPARENZ_CLIENT_SECRET` | `PDB_ALLOW_NETZTRANSPARENZ` | keine Lizenz veröffentlicht, Bedingungen bei den ÜNB erfragen |
| Energy-Charts (Fraunhofer ISE) | Preis, Erzeugung, PV-Prognose | nein | – | `PDB_ALLOW_ENERGY_CHARTS` | nur nicht-kommerziell; Börsenpreise (EPEX/EEX) nicht weitergebbar |
| EIA, Reihen von Thomson Reuters (`eia_reuters`) | Brent, WTI, Benzin, Heizöl, Kerosin, Propan, Henry Hub | empfohlen (kostenlos) | `PDB_EIA_API_KEY` | `PDB_ALLOW_EIA_REUTERS` | urheberrechtlich geschützt (Thomson Reuters) |
| EIA, eigene Reihen (`eia`) | ULSD (Diesel/Gasöl) | empfohlen (kostenlos) | `PDB_EIA_API_KEY` | – (frei) | keine, Public Domain |
| Trading Hub Europe (`the`) | Tagespreis Gas am VTP, Ausgleichsenergiepreise | nein | – | `PDB_ALLOW_THE` | Vervielfältigung nur mit schriftlicher Zustimmung von THE |

Nicht angebunden ist die EEX (CO2-Auktionen, Strom-Terminpreise): Ihr Disclaimer erlaubt nur eine
einzelne persönliche Kopie, eine maschinelle Nutzung braucht ein kostenpflichtiges Abo.

## Je Quelle

### ENTSO-E Transparency Platform (`entsoe`)

1. Konto auf <https://transparency.entsoe.eu> anlegen.
2. E-Mail an `transparency@entsoe.eu` mit dem Betreff „Restful API access“ und der
   Konto-E-Mail-Adresse schicken. Nach der Freischaltung steht der Token im Kontoprofil
   („Web API Security Token“).
3. In der `.env`:

   ```dotenv
   PDB_ENTSOE_TOKEN=<Token>
   PDB_ALLOW_ENTSO_E=true
   ```

**Lizenz:** Nutzungsbedingungen vom 29.03.2023, Ziff. 3.1: Rechte der Primärdateninhaber (ÜNB,
Börsen) dürfen nicht verletzt werden, sonst ist deren Zustimmung nötig; Quelle „ENTSO-E
Transparency Platform“ nennen. Frei (CC BY 4.0) ist nur die Freigabeliste (Stand 18.10.2023).
Von den abgerufenen Reihen steht nur die Day-Ahead-Lastprognose (6.1.b) darauf, nicht Ist-Last
(6.1.a), Day-Ahead-Preis (12.1.d), Erzeugung (16.1) und Wind/PV-Prognose (14.1.d).

### Netztransparenz.de (`netztransparenz`)

1. Konto im API-Portal <https://api-portal.netztransparenz.de> anlegen.
2. Dort einen Client anlegen (OAuth2 Client Credentials); das Portal zeigt Client-ID und
   Client-Secret.
3. In der `.env`:

   ```dotenv
   PDB_NETZTRANSPARENZ_CLIENT_ID=<Client-ID>
   PDB_NETZTRANSPARENZ_CLIENT_SECRET=<Client-Secret>
   PDB_ALLOW_NETZTRANSPARENZ=true
   ```

4. Historie nachladen: `energy-ingest --source netztransparenz backfill 2011-12-01 <Folgemonat>`.

**Lizenz:** Weder auf der Website noch in der WebAPI-Doku (v1.14) ist eine Lizenz zu finden. Wer
die Daten nutzen oder weitergeben will, fragt die Bedingungen bei den ÜNB (50Hertz, Amprion,
TenneT, TransnetBW) an. Ohne Freigabe rechnet `pdb_marktwert` die Marktwerte aus SMARD selbst
(Abweichung siehe `docs/analysen/marktwert-schaetzung`).

### Energy-Charts (`energy_charts`)

Kein Schlüssel nötig, `api.energy-charts.info` ist offen.

```dotenv
PDB_ALLOW_ENERGY_CHARTS=true
```

**Lizenz:** Daten frei für nicht-kommerzielle und journalistische Zwecke mit Nennung
„Energy-Charts / Fraunhofer ISE“. Spot- und Terminpreise sind von EPEX SPOT/EEX lizenziert und
dürfen nicht weitergegeben werden. Für kommerzielle Nutzung den Fraunhofer ISE fragen.

### EIA (`eia`, `eia_reuters`)

1. Kostenlosen Schlüssel unter <https://www.eia.gov/opendata/register.php> beantragen; er kommt
   per E-Mail.
2. In der `.env`:

   ```dotenv
   PDB_EIA_API_KEY=<Schlüssel>
   # nur wenn die Reuters-Reihen gewollt und zulässig sind
   PDB_ALLOW_EIA_REUTERS=true
   ```

Ohne Schlüssel lädt die Quelle die Bulk-Dateien `PET.zip` und `NG.zip` (zusammen rund 60 MB je
Abruf); mit Schlüssel nutzt sie die API v2 für laufende Updates.

**Lizenz:** Reihen, die die EIA selbst erhebt (ULSD), sind Public Domain und laufen immer. Brent,
WTI, Benzin, Heizöl, Kerosin, Propan und Henry Hub bezieht die EIA von Thomson Reuters; sie sind
urheberrechtlich geschützt. Freie Ersatzreihen liefern die Weltbank (monatlich) und das EU Weekly
Oil Bulletin (wöchentlich).

### Trading Hub Europe (`the`)

Kein Schlüssel nötig; die DataExport-API verlangt nur einen aussagekräftigen User-Agent, den der
Stack mitschickt.

```dotenv
PDB_ALLOW_THE=true
```

**Lizenz:** Laut Disclaimer braucht jede Vervielfältigung der Daten die schriftliche Zustimmung
von THE. Vor dem Einschalten Zustimmung bei THE einholen.

## Freie Quellen ohne Schlüssel

Diese Quellen laufen ohne Registrierung und Schalter; verlangt ist nur der Quellenvermerk:

| Quelle | Lizenz |
|---|---|
| NOAA GFS/GEFS | gemeinfrei |
| DWD Open Data, ECMWF Open Data | CC BY 4.0 |
| Marktstammdatenregister (MaStR) | Datenlizenz Deutschland – Namensnennung 2.0 |
| SMARD (Bundesnetzagentur) | CC BY 4.0, „Bundesnetzagentur \| SMARD.de“ |
| `pdb_marktwert` | eigene Berechnung aus SMARD |
| EZB (EUR/USD) | Weiterverwendung mit Quellenangabe |
| Weltbank Pink Sheet, EU Weekly Oil Bulletin | CC BY 4.0 |

## Checkliste für ein neues System

1. `.env.example` nach `.env` kopieren; alle `PDB_ALLOW_*` stehen auf `false`.
2. Optional, aber empfohlen: EIA-Schlüssel beantragen und `PDB_EIA_API_KEY` setzen.
3. Für jede gewünschte unfreie Quelle: Lizenzfrage klären, Zugang beantragen (oben), Variablen
   setzen, Schalter auf `true`.
4. Stack neu starten; `energy-ingest sources` listet die aktiven Quellen und ob sie frei oder
   unfrei, aber freigegeben sind.
5. Geheimnisse gehören nur in die `.env` (chmod 600) oder einen Secret-Store, nie ins Repo.
