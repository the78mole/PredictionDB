# Datenblatt: EUA-Auktionsreihe der gemeinsamen Auktionsplattform (CAP)

Bereinigte Primärmarkt-Auktionsreihe für EU-Emissionszertifikate (EUA), einmalig aus den
Quartals- und Monatsberichten der EU-Kommission übernommen. Erzeugt von
[`../auktionen.py`](../auktionen.py), Stand 28.10.2026.

## Quelle und Lizenz

- **Herausgeber:** Europäische Kommission, DG CLIMA, Berichte "Auctions by the Common Auction
  Platform" (`cap_report_YYYYMM_en.pdf`), verlinkt auf der
  [Auktionierungsseite](https://climate.ec.europa.eu/eu-action/eu-emissions-trading-system-eu-ets/auctioning_en).
- **Lizenz:** CC BY 4.0 nach Beschluss 2011/833/EU über die Weiterverwendung von
  Kommissionsdokumenten ([Legal notice](https://commission.europa.eu/legal-notice_en)).
  Berechnungen und die Veröffentlichung abgeleiteter Daten sind mit Quellenangabe erlaubt.
- **Attribution:** "Quelle: Europäische Kommission, DG CLIMA, Auctions by the Common Auction
  Platform; Daten der EEX als Auktionsplattform; bearbeitet von PredictionDB."
- **Abgrenzung:** Es werden nur die PDF-Berichte der Kommission gelesen, keine Daten der EEX
  selbst. Die Auktionen der deutschen Plattform (EEX für Deutschland, Berichte der DEHSt) sind
  nicht enthalten.
- **Einmaliger Import:** Die Berichte sind PDFs mit wechselnder Gestaltung. Nach den
  Projektregeln ist das für eine einmalige Analyse zulässig, nicht aber als regelmäßiger
  Datenbank-Input.

## Dateien

| Datei | Inhalt |
|---|---|
| `eua_auktionen.csv` | eine Zeile je Auktion (1.761 Zeilen) |
| `eua_tagesreihe.csv` | eine Zeile je Auktionstag (1.695 Zeilen), volumengewichtet |
| `eua_berichte.csv` | ein Eintrag je Bericht (56) mit PDF-Datum und Verzug |
| `eua_import_protokoll.txt` | Zählung des Imports |
| `../tabellen/abgleich_berichtsmittel.csv` | Abgleich mit Monats- und Jahresmitteln aus Table 1 |

### `eua_auktionen.csv`

| Spalte | Einheit | Bedeutung |
|---|---|---|
| `datum` | Datum | Auktionstag |
| `art` | | `general` (Allgemeinzertifikate) oder `aviation` (Luftverkehr, EUAA) |
| `plattform` | | `CAP`, bei Auktionen für Polen über die CAP `PL` |
| `bericht` | | Bericht, aus dem die Zeile stammt |
| `preis` | €/t | Zuschlagspreis (auction clearing price) |
| `gebot_min`, `gebot_max`, `gebot_mittel`, `gebot_median` | €/t | Gebotsstatistik laut Annex 2, leer wenn nicht gedruckt |
| `cover` | | Überzeichnung (cover ratio) |
| `volumen` | t | versteigerte Menge |
| `gebotsvolumen` | t | Gebotsmenge, nur wenn aus Cover × Volumen bestätigt |
| `erloes` | € | Erlös |
| `pruefung` | | `ok`, `korrigiert` oder `abgeleitet` (siehe unten) |
| `korrektur` | | Begründung bei Korrektur oder Ableitung |

### `eua_tagesreihe.csv`

Nur Allgemeinzertifikate. `preis` ist das volumengewichtete Mittel der Auktionen des Tages,
`volumen` die Summe, `auktionen` die Anzahl, `cover` das volumengewichtete Cover. `bericht`
und `korrektur` werden übernommen.

### `eua_berichte.csv`

`pdf_datum` ist das Änderungsdatum aus den PDF-Metadaten (`ModDate`) und dient als Näherung
für den Tag der Veröffentlichung. `verzug_tage` ist der Abstand zwischen der letzten Auktion
des Berichts und diesem Datum. Ein echtes Veröffentlichungsdatum nennt die Kommission nicht.
Der Verzug liegt zwischen 28 und 669 Tagen, im Median bei 145 Tagen. Der jüngste Bericht
(`cap_report_202506_en`, Q2 2025) trägt das Datum 25.08.2025.

## Verfahren

1. Die Seite der Kommission wird nach `cap_report_*` ab 2015 durchsucht, die PDFs werden
   heruntergeladen und mit `pdftotext -layout` in Text umgewandelt.
2. In Annex 2 wird jede Auktionszeile gelesen. Datum und Zahlen stehen je Bericht in anderen
   Formaten (Tausendertrennung mit Leerzeichen, Komma oder ohne; Datum als `1 /6 /2015`,
   `09.01.2017` oder `03-10-22`). Das Volumen wird unter den Ganzzahlen der Zeile so gewählt,
   dass Preis × Volumen den Erlös ergibt. Das Gebotsvolumen muss zum Cover passen.
   Umbrochene Zeilen und Seitenwechsel werden über die folgenden Zeilen zusammengesetzt.
3. Abgesagte Auktionen (3) werden erkannt und ausgelassen. Luftverkehrsauktionen, die ein
   Bericht zusätzlich in der allgemeinen Tabelle führt, werden einmal gezählt.
4. Doppelte Zeilen aus sich überlappenden Berichten werden entfernt (1.766 Rohzeilen, 1.761
   bereinigt). Keine Zeile blieb unlesbar.

## Korrekturen

Jede Korrektur ist durch Zahlen aus demselben Bericht belegt:

| Datum | Befund | Korrektur |
|---|---|---|
| 26.01.2015 | Preis 7,76 gedruckt, Erlös 19.725.680 / Volumen 2.918.000 = 6,76; Mittel und Median der Gebote 6,75/6,74 | Preis 6,76 €/t |
| 29.03.2017 | Erlös 20.028.335 gedruckt, 4,71 × 5.738.500 = 27.028.335; das Monatsmittel passt mit 4,71 | Erlös 27.028.335 € |
| 28.02.2017 | Auktion steht im Auktionskalender, fehlt aber in Annex 2 | Preis 5,15 €/t aus (Monatserlös laut Table 1 − übrige Erlöse) / Volumen 4.261.500 |

## Abgleich mit den Berichtsmitteln

Table 1 jedes Berichts nennt das volumengewichtete Monatsmittel und das Monatsvolumen der
Allgemeinzertifikate, ab dem Jahresbericht auch das Jahresmittel.

- **Monate:** alle 125 Monate von 01/2015 bis 06/2025 stimmen. Die größte Preisabweichung ist
  0,01 €/t (Rundung), alle Volumina stimmen exakt.
- **Jahre:** 2015 bis 2024 stimmen in Mittel und Volumen. Für 2025 liegt nur das erste Halbjahr
  vor, ein Jahresmittel ist nicht gedruckt.

Ohne die drei Korrekturen würden die Monate 01/2015, 02/2017 und 03/2017 nicht stimmen.

## Bekannte Grenzen

- Die Reihe endet am 30.06.2025, weil seither kein Bericht erschienen ist (Stand 28.10.2026).
- Der Plattformwechsel Ende 2026 ist nicht abgebildet. Ob die Kommission die Berichtsreihe
  danach im selben Format fortführt, ist offen.
- Das PDF-Datum ist nur eine Näherung. Wurde ein PDF nachträglich geändert, unterschätzt die
  Reihe die Verfügbarkeit, der Verzug ist dann eher zu lang.
- Die Weltbank-Werte zum 1. April (Carbon Pricing Dashboard) konnten nicht abgeglichen werden,
  weil der Abruf mit HTTP 403 abgewiesen wurde.

## Neu erzeugen

Aus dem Repository-Wurzelverzeichnis:

```bash
uv run --no-project --with pandas --with httpx \
    python docs/analysen/eua-rueckrechnung/auktionen.py --cache /tmp/eua
```

`pdftotext` und `pdfinfo` (poppler-utils) müssen installiert sein.
