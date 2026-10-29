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
| NCAR GDEX d084001 | GFS-Historie vor v16 (bis 2021-03-22 06Z, nur `gfs-history.yml` mit `source: gdex`) | nein | CC BY 4.0, „NCEP GFS 0.25 Degree Global Forecast Grids Historical Archive, Research Data Archive at NCAR, doi:10.5065/D65D8PWK“ |
| Marktstammdatenregister (MaStR) | installierte Leistung PV/Wind | nein | Datenlizenz Deutschland – Namensnennung 2.0 |
| SMARD (Bundesnetzagentur) | Day-Ahead-Preis, Last, Erzeugung, Prognosen | nein | CC BY 4.0, „Bundesnetzagentur \| SMARD.de“ |
| Energy-Charts (Fraunhofer ISE) | Day-Ahead-Preis DE-LU und 14 Nachbarzonen, Erzeugung, Last, PV/Wind-Prognose, Handel und physikalische Flüsse je Grenze | nein | CC BY 4.0, „Energy-Charts.info“; Preise DE-LU und Nachbarzonen: CC BY 4.0, „Bundesnetzagentur \| SMARD.de“ |
| ENTSO-E, freier Teil (`entsoe_last`) | Day-Ahead-Lastprognose (6.1.b) für DE und 11 Nachbarzonen | Token (siehe unten) | CC BY 4.0, „ENTSO-E Transparency Platform“ |
| `pdb_marktwert` | Monats- und Jahresmarktwerte | – | eigene Berechnung aus SMARD |
| EIA, eigene Reihen (`eia`) | ULSD (Diesel/Gasöl) | optional, kostenlos | Public Domain |
| EIA Short-Term Energy Outlook (`eia_steo`) | Monatsprognosen Brent und Henry Hub je Ausgabe | nein | Public Domain (nur die Prognosen; die Ist-Werte von Thomson Reuters in derselben Tabelle werden nicht übernommen) |
| EZB | Referenzkurs EUR/USD | nein | Weiterverwendung mit Quellenangabe |
| Weltbank Pink Sheet | Monatspreise Öl, Kohle, Gas (TTF), LNG | nein | CC BY 4.0 |
| EU Weekly Oil Bulletin | Endkundenpreise Kraftstoffe, Heizöl | nein | CC BY 4.0 |
| GIE AGSI+ (`gie_agsi`) | Gasspeicher je Gastag: Füllstand, Füllgrad, Ein-/Ausspeisung (DE, AT, NL, CZ, EU) | ja, kostenlos (siehe unten) | frei weiterverwendbar, Quellenangabe „GIE AGSI+“ Pflicht |
| GIE ALSI (`gie_alsi`) | LNG-Terminals je Gastag: Bestand, Ausspeisung, Kapazität (DE, NL, BE, FR, EU) | wie AGSI+ | wie AGSI+, Quellenangabe „GIE ALSI“ |
| Eurostat (`eurostat`) | monatlich: Gas (Bestand, Bilanz, Importe nach Herkunft), Rohöl-Einfuhrpreis DE, Öl (Bilanz, Bestände, Rohöl nach Herkunft), Steinkohle (Einfuhr, Kraftwerkseinsatz, EU-Bestand) | nein | CC BY 4.0, „Eurostat“ |

### Energy-Charts: Lizenz je Reihe

Laut API-Dokumentation (v2.0) stehen die Daten von `api.energy-charts.info` unter CC BY 4.0,
soweit nicht anders angegeben. Abweichend geregelt ist nur der Day-Ahead-Preis (`/price`): Die
Antwort nennt die Lizenz im Feld `license_info`. Für DE-LU und die Zonen AT, BE, CH, CZ, DK1,
DK2, FR, HU, IT-North, NL, NO2, PL, SE4 und SI ist das CC BY 4.0 von SMARD; für alle anderen
Zonen nur private und interne Nutzung. `energy-ingest` lädt DE-LU und alle Nachbarzonen mit
CC BY 4.0 (Reihen `da_price_<zone>`, z. B. `da_price_fr`), prüft den Vermerk bei jeder Antwort und
schreibt keine Preise, wenn er fehlt oder abweicht.

### EIA-Schlüssel (empfohlen)

Die freien EIA-Reihen laufen auch ohne Schlüssel über die Bulk-Dateien `PET.zip` und `NG.zip`
(zusammen rund 60 MB je Abruf). Mit Schlüssel nutzt die Quelle die API v2 für laufende Updates.

1. Kostenlosen Schlüssel unter <https://www.eia.gov/opendata/register.php> beantragen; er kommt
   per E-Mail.
2. In der `.env`: `PDB_EIA_API_KEY=<Schlüssel>`

### GIE-AGSI+-Schlüssel (empfohlen)

Derselbe Schlüssel gilt für AGSI+ (Speicher) und ALSI (LNG). Ohne Schlüssel bleiben
`gie_agsi` und `gie_alsi` aus (Warnung `energy.gie_disabled`); Speicher und Importe gibt es
dann nur monatlich von Eurostat.

1. Konto unter <https://agsi.gie.eu/account> anlegen; der Schlüssel steht danach im Konto. Er
   läuft nicht ab und gilt für AGSI+ und ALSI.
2. In der `.env`: `PDB_GIE_API_KEY=<Schlüssel>`

Bedingungen: Die Daten dürfen beliebig weiterverwendet werden, die Quelle („GIE AGSI+“) muss
aber genannt sein; sonst kann GIE den Schlüssel sperren. Höchstens 60 Abrufe je Minute, der
Stack hält dafür gut eine Sekunde Abstand. Historie vor 2015: `energy-ingest --source gie_agsi
backfill 2011-01-01 2015-01-01`.

## Unfreie Quellen (nur intern, mit Schalter)

| Quelle | Wofür | Schlüssel | Schalter | Warum unfrei |
|---|---|---|---|---|
| ENTSO-E Transparency Platform (`entsoe`) | Preis, Ist-Last, Erzeugung, Wind/PV-Prognose (Abgleich zu SMARD) | Token | `PDB_ALLOW_ENTSO_E` | Preis, Ist-Last, Erzeugung, Wind/PV-Prognose nicht auf der Free-Re-use-Liste |
| Netztransparenz.de (`netztransparenz`) | amtliche Monats- und Jahresmarktwerte | OAuth2-Client | `PDB_ALLOW_NETZTRANSPARENZ` | keine Lizenz veröffentlicht |
| EIA, Reihen von Thomson Reuters (`eia_reuters`) | Brent, WTI, Benzin, Heizöl, Kerosin, Propan, Henry Hub | wie EIA | `PDB_ALLOW_EIA_REUTERS` | urheberrechtlich geschützt |
| Trading Hub Europe (`the`) | Tagespreis Gas am VTP, Ausgleichsenergiepreise | nein | `PDB_ALLOW_THE` | Vervielfältigung nur mit schriftlicher Zustimmung |

Nicht angebunden ist die EEX (CO2-Auktionen, Strom-Terminpreise): Ihr Disclaimer erlaubt nur
eine einzelne persönliche Kopie.

Für den Stack sind diese Quellen verzichtbar: SMARD und Energy-Charts liefern Preis, Last und
Erzeugung frei, `pdb_marktwert` die Marktwerte, Weltbank und Oil Bulletin die Brennstoffpreise.

### ENTSO-E Transparency Platform

Der Token schaltet die freien Day-Ahead-Lastprognosen (`entsoe_last`, Deutschland und
Nachbarzonen) ohne weiteren Schalter frei.
Für Preis, Ist-Last, Erzeugung und Wind/PV-Prognose (`entsoe`) braucht es zusätzlich
`PDB_ALLOW_ENTSO_E=true`.

1. Konto auf <https://transparency.entsoe.eu> anlegen.
2. E-Mail an `transparency@entsoe.eu`, Betreff „Restful API access“, mit der
   Konto-E-Mail-Adresse. Nach der Freischaltung steht der Token im Kontoprofil.
3. In der `.env`:

   ```dotenv
   PDB_ENTSOE_TOKEN=<Token>
   # nur für den unfreien Teil, danach keine freie Veröffentlichung
   PDB_ALLOW_ENTSO_E=true
   ```

Lizenz: Nutzungsbedingungen vom 29.03.2023, Ziff. 3.1: Rechte der Primärdateninhaber (ÜNB,
Börsen) beachten, sonst ist deren Zustimmung nötig. Frei (CC BY 4.0) ist nur die „List of Data
available for free re-use“ (Stand 18.10.2023). Von den abgerufenen Reihen steht nur die
Day-Ahead-Lastprognose (6.1.b) darauf; sie läuft deshalb als eigene freie Quelle.

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
| Nettoerzeugung inkl. Industrie-Eigenversorgung | Energy-Charts `/total_power` | vollständigere Erzeugungs- und Lastbilanz |
| Installierte Leistung je Energieträger | Energy-Charts `/installed_power` | Plausibilitätsprüfung zu MaStR, Kraftwerkspark konventionell |
| Lastprognosen Woche/Monat/Jahr voraus, Übertragungskapazitäten | ENTSO-E (Free-Re-use-Liste, Token nötig) | längere Vorläufe, Engpässe an den Grenzen |
| Wetter-Beobachtungen und Prognosen | DWD Open Data, ECMWF Open Data (CC BY 4.0) | Ergänzung und Abgleich zu GFS |

## Anlagenbestand der Nachbarländer

In Deutschland gewichtet `wx-process` die Wetterprognose je Anlagen-Cluster mit der Leistung aus
dem MaStR (siehe `docs/ARCHITEKTUR.md`, 7.5). Für eine grobe Clusterung der Nachbarländer
braucht man je Land zweierlei: die **installierte Leistung je Technologie** über die Zeit und eine
**räumliche Verteilung**, die fein genug für einige wenige Cluster ist. Ein Register mit
Einzelanlagen und Koordinaten wie das MaStR gibt es frei nur in der Schweiz. Für die übrigen
Länder reichen Summen je Gemeinde, Provinz oder Region; bei 0,25°-Gittern und zwei bis fünf
Clustern je Land ist das genau genug.

Stand der Bewertung: Oktober 2026. Es zählen nur Quellen mit freier Lizenz, die sich regelmäßig
und ohne Handarbeit per API oder fester Datei-URL laden lassen. Noch ist keine davon eingebunden.

### Je Land

| Land | Beste freie Quelle | Inhalt | Zugriff, Takt | Lizenz | Eignung |
|---|---|---|---|---|---|
| FR | Registre national des installations de production et de stockage (RTE, Enedis u. a.) auf ODRÉ und data.gouv.fr | alle Anlagen mit Filière, Leistung, Datum, Gemeinde (INSEE) und IRIS; unter 36 kW nur je IRIS zusammengefasst | Opendatasoft-API bzw. CSV, mehrere Stichtage im Jahr | Licence Ouverte 2.0 | gut; Koordinaten über Gemeinde-Mittelpunkt |
| CH | Elektrizitätsproduktionsanlagen (BFE, Pronovo) auf opendata.swiss | rund 340 000 Einzelanlagen mit Kategorie, Leistung, Inbetriebnahme, Adresse und Koordinaten (LV95), PV mit Ausrichtung und Neigung | feste URL `data.geo.admin.ch/…/elektrizitaetsproduktionsanlagen_2056.csv.zip`, STAC-API, monatlich | opendata.swiss „Open use. Must provide the source“ | sehr gut, MaStR-gleich; nur Anlagen im Herkunftsnachweissystem |
| DK | Energi Data Service `CapacityPerMunicipality` (Energinet) | Leistung und Anzahl je Gemeinde: Wind an Land, Wind auf See, PV, thermisch ≥/< 100 MW | REST-API ohne Schlüssel, monatlich | „Conditions for use of Danish public-sector data“: frei, auch kommerziell, Quellenvermerk | sehr gut |
| NL | CBS StatLine 85005NED (PV je Gemeinde) und 70960ned (Wind an Land je Provinz) | PV-Leistung und Anlagen je Gemeinde, Wind je Provinz | OData-API, PV halbjährlich, Wind jährlich, rund ein Jahr Verzug | CC BY 4.0 | gut für PV, grob für Wind; Wind auf See über die Windparks (fest, wenige) |
| BE | Elia Open Data `ods032`/`ods209` (PV) und `ods031` (Wind) | `monitoredcapacity` je Provinz (PV) bzw. Region und an Land/auf See (Wind), als Viertelstundenreihe | Opendatasoft-API, laufend | „Elia Open Data Licence“ (laut JRC CC BY 4.0, Text vor Einbau prüfen) | gut |
| NO | NVE Vindkraft-API (`api.nve.no/web/WindPowerplant/GetWindPowerPlants`), NVE Vannkraft (WMS/Download) | Windparks mit Leistung, Kommune, Fylke, Elspot-Gebiet, Nabenhöhe; Wasserkraftwerke mit Lage | REST-API ohne Schlüssel, laufend | NLOD (frei, Quellenvermerk) | gut |
| SE | Vindbrukskollen (Länsstyrelserna) | Windenergieanlagen mit Koordinaten und Leistung | Shape-Download | laut Geodatakatalog CC0, Länsstyrelsen bittet um Quellenangabe | gut für Wind; PV je Gemeinde (Energimyndigheten, PxWeb) noch prüfen |
| AT | kein offenes Register | E-Control veröffentlicht PV je Bundesland, aber als Excel ohne Lizenzangabe | – | – | nur Landessumme; Verteilung über Ersatzgrößen |
| CZ | kein offenes Register | ERÚ-Berichte je Kraj nur als PDF | – | – | nur Landessumme; Verteilung über Ersatzgrößen |
| PL | URE-Register der kleinen Anlagen auf dane.gov.pl (CC0) | nur 50 kW bis 1 MW, seit 2023 nicht aktualisiert; Mikroanlagen (der Großteil der PV) fehlen | – | CC0 | ungeeignet; Landessumme und Ersatzgrößen |
| IT | Terna Download Center, GSE Atlaimpianti | Leistung je Region (Terna) bzw. Einzelanlagen (GSE) | Download bzw. Webportal | ungeklärt bzw. ohne offene Lizenz | nur Landessumme; für IT-North Regionen nachprüfen |
| LU | – | gehört zur Gebotszone DE-LU, wenige hundert MW | – | – | Landessumme genügt |

### Landessummen je Technologie

| Quelle | Länder | Takt, Verzug | Lizenz | Bemerkung |
|---|---|---|---|---|
| Energy-Charts `/installed_power?country=…` | alle zwölf, auch CH | jährlich, bei DK und SE schon 2026 | CC BY 4.0 (kein abweichender `license_info`) | für die Nachbarländer meist aus ENTSO-E 14.1.a übernommen; einfachste Wahl |
| Eurostat `nrg_inf_epc` | alle außer CH | jährlich, gut ein Jahr Verzug | CC BY 4.0 | amtlich, Redundanz und Abgleich |
| ENTSO-E 14.1.a (installierte Leistung je Gebotszone) | alle | jährlich | nicht als frei bestätigt, deshalb nicht direkt | über Energy-Charts abgedeckt |
| Ember Monthly Wind and Solar Capacity | EU-Länder | monatlich | CC BY 4.0, Schlüssel nötig | Zwischenwerte zwischen den Jahresständen |

Zwischen zwei Jahresständen wird linear interpoliert, wie bei `assets.capacity_daily`.

### Ersatzgrößen für die Verteilung

Wo kein Register frei verfügbar ist (AT, CZ, PL, IT), verteilt man die Landessumme über
Ersatzgrößen:

- **Wind:** Standorte aus OpenStreetMap (`power=generator` mit `generator:source=wind`, über die
  Overpass-API). Die Erfassung ist in Mitteleuropa gut. Die Lizenz ODbL verlangt aber, dass eine
  daraus abgeleitete Datenbank wieder unter ODbL steht. Für veröffentlichte Cluster-Gewichte ist
  das mit CC BY 4.0 nicht vereinbar; man nutzt OSM deshalb nur für die Clustergrenzen und
  veröffentlicht die Gewichte nicht, oder man verzichtet darauf.
- **PV:** Bevölkerung je Rasterzelle (Eurostat GEOSTAT 1 km, CC BY 4.0). Dachanlagen folgen der
  Besiedlung, große Freiflächenanlagen weniger; für wenige Cluster je Land reicht das.
- **Global Energy Monitor** (Wind- und Solar-Tracker, CC BY 4.0, Parks ab etwa 10 MW bzw. 1 bis
  20 MW mit Koordinaten): gut, aber der Download geht nur über ein Formular und die URL wechselt
  je Ausgabe. Deshalb nur für einmalige Analysen und zum Abgleich, nicht als laufende Quelle.
- **Open Power System Data** `renewable_power_plants` (Einzelanlagen DE, DK, FR, CH, SE, CZ,
  PL je Powiat): letzte Ausgabe 2020, nur noch für die Historie bis dahin.

### Grenzen durch den Wetter-Ausschnitt

Der GFS-Ausschnitt in `config/sources/gfs_0p25.yaml` reicht von 44° bis 58° N und von 0° bis
20° O. Er deckt NL, BE, LU, CH, AT, CZ und DK ganz ab, FR ohne die Bretagne und den äußersten
Süden, Norditalien bis 44° N und Polen nur westlich von 20° O. Norwegen (NO1 bis NO5) und
Schweden außer SE4 liegen fast ganz außerhalb. Wer dort Cluster bilden will, muss den
Ausschnitt erweitern; das vergrößert Silber und die Historie entsprechend.

## Checkliste für ein neues System

1. `.env.example` nach `.env` kopieren; alle `PDB_ALLOW_*` bleiben auf `false`, wenn Daten
   oder Modelle veröffentlicht werden sollen.
2. Empfohlen: EIA- und GIE-AGSI+-Schlüssel beantragen und `PDB_EIA_API_KEY` bzw.
   `PDB_GIE_API_KEY` setzen.
3. Nur für internen Gebrauch: gewünschte unfreie Quelle beantragen (oben), Variablen setzen,
   Schalter auf `true`. Danach keine Daten oder Modelle dieser Instanz veröffentlichen.
4. Stack starten; `energy-ingest sources` listet die aktiven Quellen und ob sie frei oder
   unfrei, aber freigegeben sind.
5. Geheimnisse gehören nur in die `.env` (chmod 600) oder einen Secret-Store, nie ins Repo.
