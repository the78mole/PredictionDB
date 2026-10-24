# Datenquellen: Schlüssel, Lizenzen und freie Veröffentlichung

Diese Anleitung richtet sich an Betreiber, die den Stack neu aufsetzen. Sie beschreibt, welche
Datenquellen einen API-Schlüssel brauchen, welche Quellen unfrei sind und was das für die
Veröffentlichung von Daten und Modellen bedeutet.

Die Einordnung der Bedingungen ist eine technische Einschätzung, keine Rechtsberatung.

## Ziel: freie Daten, freie Modelle

PredictionDB soll Daten und trainierte Modelle öffentlich und frei verfügbar machen (CC BY 4.0,
siehe `docs/ARCHITEKTUR.md`). Daraus folgen zwei Regeln:

1. **Alles, was frei verfügbar ist und für Last- und Preisprognose gebraucht wird, wird
   eingebunden** und läuft ohne Schalter.
2. **Nichts Unfreies fließt standardmäßig ein.** Unfreie Quellen sind nur für den internen
   Gebrauch gedacht und liegen hinter einem Schalter `PDB_ALLOW_*` mit der Vorgabe `false`.

Ein Stack in Standardeinstellung nutzt also nur freie Reihen. Seine Daten und Modelle darf der
Betreiber unter Nennung der Quellen frei veröffentlichen.

> **Wer einen Schalter `PDB_ALLOW_*` auf `true` setzt, darf seine Ergebnisse nicht mehr frei
> veröffentlichen.** Daten unfreier Quellen fließen dann in `energy.value_best`, also in
> Training, Dashboards und Exporte. Modelle und Datenexporte aus einer solchen Instanz sind nur
> für den eigenen, internen Gebrauch bestimmt. Setzt man den Schalter zurück auf `false`, fallen
> die unfreien Werte wieder aus `energy.value_best` heraus (sie bleiben in `energy.value`
> gespeichert). Erst ein danach neu trainiertes Modell ist wieder veröffentlichbar.

## Freie Quellen (Standard, ohne Schalter)

| Quelle | Wofür | Schlüssel | Lizenz und Quellenvermerk |
|---|---|---|---|
| NOAA GFS/GEFS | Wetterprognosen | nein | gemeinfrei |
| Marktstammdatenregister (MaStR) | installierte Leistung PV/Wind | nein | Datenlizenz Deutschland – Namensnennung 2.0 |
| SMARD (Bundesnetzagentur) | Day-Ahead-Preis, Last, Erzeugung, Prognosen | nein | CC BY 4.0, „Bundesnetzagentur \| SMARD.de“ |
| Energy-Charts (Fraunhofer ISE) | Day-Ahead-Preis DE-LU, Erzeugung, Last, PV/Wind-Prognose | nein | CC BY 4.0, „Energy-Charts.info“; Preis DE-LU: CC BY 4.0, „Bundesnetzagentur \| SMARD.de“ |
| `pdb_marktwert` | Monats- und Jahresmarktwerte | – | eigene Berechnung aus SMARD |
| EIA, eigene Reihen (`eia`) | ULSD (Diesel/Gasöl) | optional, kostenlos | Public Domain |
| EZB | Referenzkurs EUR/USD | nein | Weiterverwendung mit Quellenangabe |
| Weltbank Pink Sheet | Monatspreise Öl, Kohle, Gas (TTF), LNG | nein | CC BY 4.0 |
| EU Weekly Oil Bulletin | Endkundenpreise Kraftstoffe, Heizöl | nein | CC BY 4.0 |

### Energy-Charts: Lizenz je Reihe

Laut API-Dokumentation (v2.0) stehen die Daten von `api.energy-charts.info` unter CC BY 4.0,
soweit nicht anders angegeben. Abweichend geregelt ist nur der Day-Ahead-Preis (`/price`): Die
Antwort nennt die Lizenz im Feld `license_info`. Für DE-LU und die Zonen AT, BE, CH, CZ, DK1,
DK2, FR, HU, IT-North, NL, NO2, PL, SE4 und SI ist das CC BY 4.0 von SMARD; für alle anderen
Zonen nur private und interne Nutzung. `energy-ingest` prüft den Vermerk bei jeder Antwort und
schreibt keine Preise, wenn er fehlt oder abweicht.

### EIA-Schlüssel (empfohlen)

Die freien EIA-Reihen laufen auch ohne Schlüssel über die Bulk-Dateien `PET.zip` und `NG.zip`
(zusammen rund 60 MB je Abruf). Mit Schlüssel nutzt die Quelle die API v2 für laufende Updates.

1. Kostenlosen Schlüssel unter <https://www.eia.gov/opendata/register.php> beantragen; er kommt
   per E-Mail.
2. In der `.env`: `PDB_EIA_API_KEY=<Schlüssel>`

## Unfreie Quellen (nur intern, mit Schalter)

| Quelle | Wofür | Schlüssel | Schalter | Warum unfrei |
|---|---|---|---|---|
| ENTSO-E Transparency Platform (`entsoe`) | Preis, Ist-Last, Lastprognose, Erzeugung, Wind/PV-Prognose (Abgleich zu SMARD) | Token | `PDB_ALLOW_ENTSO_E` | Preis, Ist-Last, Erzeugung, Wind/PV-Prognose nicht auf der Free-Re-use-Liste |
| Netztransparenz.de (`netztransparenz`) | amtliche Monats- und Jahresmarktwerte | OAuth2-Client | `PDB_ALLOW_NETZTRANSPARENZ` | keine Lizenz veröffentlicht |
| EIA, Reihen von Thomson Reuters (`eia_reuters`) | Brent, WTI, Benzin, Heizöl, Kerosin, Propan, Henry Hub | wie EIA | `PDB_ALLOW_EIA_REUTERS` | urheberrechtlich geschützt |
| Trading Hub Europe (`the`) | Tagespreis Gas am VTP, Ausgleichsenergiepreise | nein | `PDB_ALLOW_THE` | Vervielfältigung nur mit schriftlicher Zustimmung |

Nicht angebunden ist die EEX (CO2-Auktionen, Strom-Terminpreise): Ihr Disclaimer erlaubt nur
eine einzelne persönliche Kopie.

Für den Stack sind diese Quellen verzichtbar: SMARD und Energy-Charts liefern Preis, Last und
Erzeugung frei, `pdb_marktwert` die Marktwerte, Weltbank und Oil Bulletin die Brennstoffpreise.

### ENTSO-E Transparency Platform

1. Konto auf <https://transparency.entsoe.eu> anlegen.
2. E-Mail an `transparency@entsoe.eu`, Betreff „Restful API access“, mit der
   Konto-E-Mail-Adresse. Nach der Freischaltung steht der Token im Kontoprofil.
3. In der `.env`:

   ```dotenv
   PDB_ENTSOE_TOKEN=<Token>
   PDB_ALLOW_ENTSO_E=true
   ```

Lizenz: Nutzungsbedingungen vom 29.03.2023, Ziff. 3.1: Rechte der Primärdateninhaber (ÜNB,
Börsen) beachten, sonst ist deren Zustimmung nötig. Frei (CC BY 4.0) ist nur die „List of Data
available for free re-use“ (Stand 18.10.2023). Von den abgerufenen Reihen steht nur die
Day-Ahead-Lastprognose (6.1.b) darauf.

### Netztransparenz.de

1. Konto im API-Portal <https://api-portal.netztransparenz.de> anlegen.
2. Einen Client anlegen (OAuth2 Client Credentials); das Portal zeigt Client-ID und -Secret.
3. In der `.env`:

   ```dotenv
   PDB_NETZTRANSPARENZ_CLIENT_ID=<Client-ID>
   PDB_NETZTRANSPARENZ_CLIENT_SECRET=<Client-Secret>
   PDB_ALLOW_NETZTRANSPARENZ=true
   ```

4. Historie nachladen: `energy-ingest --source netztransparenz backfill 2011-12-01 <Folgemonat>`.

Lizenz: Weder auf der Website noch in der WebAPI-Doku (v1.14) ist eine Lizenz zu finden. Die
Bedingungen müsste der Betreiber bei den ÜNB (50Hertz, Amprion, TenneT, TransnetBW) erfragen.

### EIA, Reihen von Thomson Reuters

Schlüssel wie oben, dazu `PDB_ALLOW_EIA_REUTERS=true`. Die Reihen bezieht die EIA von Thomson
Reuters; sie sind urheberrechtlich geschützt.

### Trading Hub Europe

Kein Schlüssel; die API verlangt einen aussagekräftigen User-Agent, den der Stack mitschickt.
Schalter `PDB_ALLOW_THE=true`. Laut Disclaimer braucht jede Vervielfältigung die schriftliche
Zustimmung von THE.

## Freie Reihen, die noch fehlen

Diese Reihen sind frei verfügbar und für Last- und Preisprognose nützlich, werden aber noch
nicht geladen:

| Reihe | Quelle | Nutzen für die Prognose |
|---|---|---|
| Day-Ahead-Preise der Nachbarzonen (AT, BE, CH, CZ, DK1, DK2, FR, NL, PL, …) | Energy-Charts `/price`, CC BY 4.0 (SMARD) | Marktkopplung: Preisniveau der Nachbarn erklärt Importe und Exporte |
| Grenzüberschreitender Handel und physikalische Flüsse | Energy-Charts `/cbet`, `/cbpf` | Import/Export als Teil der Residuallast |
| Nettoerzeugung inkl. Industrie-Eigenversorgung | Energy-Charts `/total_power` | vollständigere Erzeugungs- und Lastbilanz |
| Installierte Leistung je Energieträger | Energy-Charts `/installed_power` | Plausibilitätsprüfung zu MaStR, Kraftwerkspark konventionell |
| Day-Ahead-Lastprognose der ÜNB | ENTSO-E 6.1.b (Free-Re-use-Liste, Token nötig) | Benchmark und Eingangsgröße für die Lastprognose; derzeit liegt die ganze Quelle hinter `PDB_ALLOW_ENTSO_E`, die freie Reihe müsste davon getrennt werden |
| Lastprognosen Woche/Monat/Jahr voraus, Übertragungskapazitäten | ENTSO-E (Free-Re-use-Liste, Token nötig) | längere Vorläufe, Engpässe an den Grenzen |
| Wetter-Beobachtungen und Prognosen | DWD Open Data, ECMWF Open Data (CC BY 4.0) | Ergänzung und Abgleich zu GFS |

## Checkliste für ein neues System

1. `.env.example` nach `.env` kopieren; alle `PDB_ALLOW_*` bleiben auf `false`, wenn Daten
   oder Modelle veröffentlicht werden sollen.
2. Empfohlen: EIA-Schlüssel beantragen und `PDB_EIA_API_KEY` setzen.
3. Nur für internen Gebrauch: gewünschte unfreie Quelle beantragen (oben), Variablen setzen,
   Schalter auf `true`. Danach keine Daten oder Modelle dieser Instanz veröffentlichen.
4. Stack starten; `energy-ingest sources` listet die aktiven Quellen und ob sie frei oder
   unfrei, aber freigegeben sind.
5. Geheimnisse gehören nur in die `.env` (chmod 600) oder einen Secret-Store, nie ins Repo.
