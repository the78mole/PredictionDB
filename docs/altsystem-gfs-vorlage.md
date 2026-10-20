# Altsystem GFS-Wetterserver – Notizen als Vorlage

Stand: 2026-10-20 · Autor: Daniel (the78mole) Glaser

Diese Notizen beschreiben, wie ein früheres, selbst betriebenes System (2012–2014) GFS-Prognosen
abgerufen, gespeichert und ausgeliefert hat. Sie dienen **nur als Vorlage** für das
Design von PredictionDB. Code aus dem Altsystem wird **nicht** übernommen.

## Ablauf

1. **Abruf:** Ein Cronjob lief viermal täglich, jeweils etwa 20 Minuten nach den Modellläufen
   00/06/12/18 UTC (`20 0,6,12,18 * * *`).
   - Quelle war NOAA NCEP `…/data/nccf/com/gfs/prod/`, später der schnellere NOMADS-Mirror
     `https://nomads.ncep.noaa.gov/pub/data/nccf/com/gfs/prod/`.
   - Geladen wurden nur Teilbereiche: Aus dem `.idx`-Inventory wurden die Byte-Ranges der gewünschten
     Felder bestimmt und per HTTP-Range-Request (curl) geholt.
   - Welche Felder geholt werden, regelte eine Liste von Inventory-IDs in der Konfiguration.
     Eine gespeicherte Kopie der Inventory-Beschreibung (`inventory.shtml`) diente dazu, Formatänderungen
     der NOAA zu erkennen.
   - Aufwand: ca. 4200 Requests und ca. 1,5 h pro Lauf.
     **Lehre:** Requests bündeln bzw. größere Ranges laden und lokal schneiden, außerdem Limits und
     Retries vorsehen.
2. **Ablage:** Je Modelllauf und Feld wurde eine umgruppierte GRIB-Datei abgelegt
   (Verzeichnis je Lauf). Eine Statusdatei (`grib_finished.txt`) markierte vollständig geladene Läufe.
3. **Extraktion:** Ein kleines C-Programm auf Basis von ECMWF grib_api lieferte für
   `Lauf(YYYYMMDDHH) Feld Lat Lon` die Zeitreihe am nächstgelegenen Gitterpunkt.
4. **Auslieferung:** Ein CGI-Skript unter Apache lieferte die Daten als Text aus:
   - `?help` – Hilfe
   - `?idlist` – verfügbare Felder (ID ↔ Kurzname)
   - `?loclist` – Standorte (Kürzel → lat/lon)
   - `?<FeldID>&<StandortID>&<YYYYMMDDHH>` – Prognosereihe
   Antwortformat: Kopfzeilen beginnen mit `#`, eine davon enthält das Laufdatum. Danach folgt je
   Prognosestunde eine Zeile `<Stunde>:<…>:<Wert>`. Fehler beginnen mit `ERROR`.

## Weiterentwicklung 2014: Werte in der Datenbank

Ab etwa 2014 lagen die extrahierten Prognosewerte nicht mehr nur als GRIB-Dateien vor, sondern in
einer MySQL-Tabelle, und zwar auf zwei Servern parallel. Belegt ist das durch tägliche Statusmails des
Datenbankstands.

- Schema der Wertetabelle: `id` (fortlaufend), `meta_id` (Verweis auf eine Metadatentabelle mit
  Feld, Standort und Lauf), `time` (Unix-Zeit des Gültigkeitszeitpunkts) und der Wert.
- Sehr schmal: eine Zeile pro Wert, bei rund 300 Meta-Einträgen und etwa 324.000 Zeilen im Juni 2014.
- Die Überwachung lief per Cron: Ein täglicher Report zeigte die jüngsten Einträge nach ID und nach
  Zeit, um ausbleibende Läufe zu erkennen.

Diese Notizen sind nur historische Einordnung. Die aktuelle PredictionDB-Architektur aus
`SPECIFICATION.md` (Container, moderne Werkzeuge) ist ausgereifter und hat Vorrang. Übernommen werden
höchstens Ideen, zum Beispiel die Trennung von Metadaten und Werten sowie das Monitoring der
Datenaktualität.

## Genutzte Felder (Inventory-IDs des damaligen pgrb2-Formats)

| ID | Feld | Bedeutung |
|---|---|---|
| 195 | TMP:2 m above ground | Lufttemperatur 2 m (T2) |
| 245 | DSWRF:surface | kurzwellige Einstrahlung abwärts (PV-relevant) |

Die Nummern hingen vom damaligen Inventory ab und sind heute nicht mehr gültig. In PredictionDB sollten
Felder deshalb über `Variable:Level` (z. B. `TMP:2 m above ground`) ausgewählt werden, nicht über
Positionsnummern.

## Hinweise für PredictionDB

- Pro Standort und Feld wurde nur ein Gitterpunkt ausgewertet. Für Deutschland-weite Strom-Prognosen
  sollten stattdessen Flächenmittel bzw. gewichtete Punkte (Last-, PV- und Wind-Schwerpunkte) gebildet werden.
- Für das Training eignen sich auch die zu jedem Lauf archivierten Prognosen (Forecast-Historie),
  nicht nur die aktuellsten Werte.
- Ob Daten fehlen oder ein Lauf noch läuft, muss explizit behandelt werden; das Altsystem hatte hier
  offene Punkte.
