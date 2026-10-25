# Preisniveau aus freien Daten: Backtest 2016–2026

Einmalige Auswertung vom 25.10.2026. Frage: Wie gut sagt ein Modell, das nur freie Daten nutzt,
das Monatsmittel des Day-Ahead-Preises (Base) und die Marktwerte Solar und Wind an Land für die
nächsten 18 Monate voraus? Hintergrund: Für Terminpreise gibt es keine freie Quelle. Der
Terminpreis ist ohnehin nur ein mäßiger Schätzer des späteren Spotpreises
([terminpreis-treffsicherheit](../terminpreis-treffsicherheit/README.md)). Die Messlatte sind
daher einfache Spot-Mittel.

## Ergebnis in Kürze

**Das Modell schlägt den einfachsten Schätzer nicht.** Über 130 Stichtage (Januar 2016 bis
Oktober 2026) und Horizonte von 1 bis 18 Monaten liegt die Base-Prognose gleichauf mit
"Monatsmittel des letzten Monats fortschreiben". MAE in EUR/MWh:

| Horizont (Monate) | Modell | Modell relativ | letzter Monat | 12-Monats-Mittel | Vorjahresmonat |
|---|---|---|---|---|---|
| 1–3 | 22,0 | 21,1 | 21,3 | 32,1 | 43,3 |
| 4–6 | 31,7 | 30,4 | 31,2 | 39,4 | 44,2 |
| 7–12 | 42,3 | 42,6 | 42,5 | 47,4 | 45,7 |
| 13–18 | 55,9 | 55,0 | 53,9 | 53,8 | 58,2 |

Ohne die Krise (Zielmonate 07/2021–06/2023):

| Horizont (Monate) | Modell | Modell relativ | letzter Monat | 12-Monats-Mittel | Vorjahresmonat |
|---|---|---|---|---|---|
| 1–3 | 10,5 | 9,2 | 9,5 | 16,1 | 26,7 |
| 4–6 | 14,6 | 14,4 | 15,1 | 21,0 | 27,4 |
| 7–12 | 22,9 | 22,7 | 22,8 | 27,9 | 28,4 |
| 13–18 | 35,7 | 35,5 | 33,0 | 32,8 | 36,3 |

**Das Niveau verhält sich wie ein Zufallspfad.** Der beste Schätzer für die nächsten Monate ist
der Stand von heute. Saison, Residuallast und Ausbau verschieben das Monatsmittel kaum
messbar. Entscheidend ist der Brennstoffpreis, und für dessen künftigen Verlauf hat das Modell
keine Information. Den Rest verdecken Wetter und Brennstoffschwankungen, die sich nicht
vorhersehen lassen.

**Bei den Marktwerten hilft das Modell nur wenig.** Solar: Bei 7–18 Monaten liegt das Modell
1–2 EUR/MWh vor dem naiven Schätzer (Wertfaktor des Vorjahresmonats mal Base des letzten
Monats), bei 1–6 Monaten gleichauf. Wind an Land: kein Vorteil, bei 13–18 Monaten sogar
schlechter. Die Tabellen dazu stehen in [tabellen/](tabellen/).

**Folgerung:**

- Für das Preisniveau als Merkmal in `ml_train` reicht vorerst der gleitende Spotpreis der
  letzten 30 Tage. Ein eigener Dienst `pdb_niveau` lohnt sich in dieser Form nicht.
- Besser werden kann die Prognose nur mit Information über den künftigen Brennstoffpreis.
  Freie Kandidaten: EIA STEO (Brent, Henry Hub, monatlich bis Ende Y+1), die Prognosen der
  Weltbank (Gas Europa, Kohle) und die Gasspeicherfüllstände von GIE AGSI+. Ob sie das
  Niveau verbessern, wäre der nächste Backtest.

## Grafiken

![Fehler nach Horizont](grafiken/fehler_horizont.png)

![Ist gegen Prognose](grafiken/verlauf.png)

Die Prognose mit 12 Monaten Vorlauf läuft dem Ist um ein Jahr hinterher, genau wie ein
fortgeschriebener Stand. 2022 hat sie den Anstieg verpasst, 2023 den Rückgang.

## Modell

An jedem Monatsersten t, nur mit Daten vor t:

1. **Brennstoffniveau C:** Median des Preises in den Stunden der letzten 30 Tage, deren
   Residuallast im oberen Viertel der letzten 365 Tage liegt. In diesen Stunden setzen Gas oder
   Steinkohle den Preis. C enthält Brennstoff- und CO2-Kosten zusammen, ohne TTF- oder
   EUA-Daten.
2. **Merit-Order-Form g:** In den 365 Tagen vor t wird der Preis jeder Stunde durch das C der
   30 Tage davor geteilt. Davon wird der Median je Residuallast-Klasse (2 GW) gebildet.
3. **Erwartete Residuallast:** Für jeden Zielmonat dieselben Kalendermonate der bis zu drei
   Vorjahre. Wind und Solar werden mit der installierten Leistung skaliert (Stand vor t, linear
   aus den letzten 12 Monaten fortgeschrieben). Die Last bleibt unverändert.
4. **Preis** je simulierter Stunde = C × g(Residuallast). Base ist das Mittel. Der Marktwert
   ist das mit der simulierten Erzeugung gewichtete Mittel. Am Ende wird über die Vorjahre
   gemittelt.

**Modell relativ:** Monatsmittel des letzten Monats × Modell(Zielmonat) / Modell(letzter
Monat). Ein falsch geschätztes C kürzt sich so heraus. Übrig bleibt nur, was das Modell über
Saison und Ausbau sagt.

**Naive Schätzer:** Monatsmittel des letzten vollständigen Monats, Mittel der letzten 12
Monate und derselbe Kalendermonat im letzten verfügbaren Jahr. Für die Marktwerte:
Wertfaktor (Marktwert / Base) des Vorjahresmonats × Base des letzten Monats.

## Daten

Alle Daten stammen aus der [Energy-Charts-API](https://api.energy-charts.info/) (Fraunhofer
ISE), Lizenz CC BY 4.0:

- `/price`: Day-Ahead-Preis DE-AT-LU bis 30.09.2018, danach DE-LU (Bundesnetzagentur |
  SMARD.de)
- `/public_power`: Last, Wind an Land, Wind auf See, Solar für Deutschland (Viertelstunden,
  zu Stundenmitteln zusammengefasst)
- `/installed_power`: installierte Leistung je Monat (Solar DC, Wind an Land, Wind auf See)

Aufruf aus dem Repository-Wurzelverzeichnis. Der erste Lauf lädt etwa 12 Jahre Stundendaten
und dauert einige Minuten:

```
uv run --no-project --with pandas --with matplotlib --with httpx \
    python docs/analysen/preisniveau-schaetzung/auswertung.py --cache /tmp/pn
```

Ergebnisse: [tabellen/prognosen.csv](tabellen/prognosen.csv) (jede Prognose mit Ist und
naiven Werten), [tabellen/kennzahlen_base.csv](tabellen/kennzahlen_base.csv),
[tabellen/kennzahlen_mw_solar.csv](tabellen/kennzahlen_mw_solar.csv),
[tabellen/kennzahlen_mw_wind_onshore.csv](tabellen/kennzahlen_mw_wind_onshore.csv),
[tabellen/ist_monate.csv](tabellen/ist_monate.csv).

## Einschränkungen

- Die installierte Leistung ist der heutige Datenstand. Nachmeldungen im MaStR sind darin
  schon enthalten, im Backtest wäre sie also etwas zu gut bekannt. Für das Ergebnis spielt das
  kaum eine Rolle, weil das Modell ohnehin nicht besser ist als naiv.
- Die Marktwerte sind mit der Ist-Erzeugung von Energy-Charts auf Stundenbasis gerechnet, nicht
  mit der Hochrechnung der ÜNB (siehe [marktwert-schaetzung](../marktwert-schaetzung/README.md)).
- Die Prognosen überlappen sich: Zielmonate aus benachbarten Stichtagen sind nicht unabhängig.
  Die Unterschiede zwischen Modell und "letzter Monat" liegen innerhalb dessen, was das
  erklären kann.
- Die Parameter (30 Tage, oberes Viertel, 2-GW-Klassen, 3 Vorjahre) wurden nicht optimiert.
  Andere Werte könnten etwas besser abschneiden. An der Grundaussage ändert das
  voraussichtlich nichts, weil auch "Modell relativ" nicht besser ist.
