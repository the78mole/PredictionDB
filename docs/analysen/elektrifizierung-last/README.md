# E-Autos, Wärmepumpen und Klimageräte: Einfluss auf Strombedarf und Lastspitzen

Einmalige Auswertung vom 26.10.2026. Zwei Fragen:

1. Wie stark haben die Zulassungen von E-Autos und die Verkäufe von Wärmepumpen und
   Klimageräten die deutsche Netzlast bis 2025 verändert?
2. Was bedeutet es für Jahresbedarf und Lastspitzen, wenn fast alle Pkw elektrisch fahren
   und Wärmepumpen überall heizen, wo keine nicht-fossile Alternative zum Einsatz kommt?
   Als Alternative zählen hier nur Wärmenetze (Biomasse, Biogas, Müllverbrennung,
   Abwärme, Flusswasser-Großwärmepumpen) und die heute schon nicht fossilen Heizungen.

Keine der Reihen geht in die Datenbank. Werte, die nur als Webseite oder PDF vorliegen,
sind von Hand mit Quelle übernommen (Liste unten und in [daten.py](daten.py)).

## Ergebnis in Kürze

**Bisher ist der Effekt klein und steckt im Rauschen.** E-Autos haben 2025 etwa 5,4 TWh
verbraucht, die Wärmepumpen der Haushalte 2024 etwa 8,1 TWh. Zusammen sind das knapp 3 % der
Netzlast von 466 TWh. Die Last ist seit 2018 trotzdem um 43 TWh gefallen, weil die Industrie
weniger verbraucht. Klimageräte sind in der Last nicht zu sehen: Eurostat weist für die
Kühlung der Haushalte 1,3 TWh aus, und die Empfindlichkeit der Sommerlast auf Hitze ist
nicht gestiegen.

**Die Winterlast reagiert heute stärker auf Kälte als 2015.** Je Kelvin Heizgradtag steigt
die Tageslast um 0,41 GW (Mittel 2015–2025). Der Wert wächst um 0,022 ± 0,006 GW je Jahr.
Aus dem Wärmepumpenbestand der Haushalte allein wären 0,010 GW je Jahr zu erwarten. Den Rest
erklären vermutlich Wärmepumpen in Gewerbe und Nichtwohngebäuden, die Eurostat nicht ausweist,
und der wachsende PV-Eigenverbrauch, der die Last an hellen Tagen senkt.

**Im Vollausbau steigt der Bedarf um rund 290 TWh auf etwa 755 TWh je Jahr (+61 %).** Davon
entfallen 157 TWh auf Wärmepumpen in Gebäuden, 17 TWh auf Großwärmepumpen in Wärmenetzen und
113 TWh auf E-Autos (Mittel über die Wetterjahre 1991–2025). Die Wärmepumpen erreichen eine
Jahresarbeitszahl von 3,2.

**Die Jahreshöchstlast steigt von gut 73 GW auf 185 GW im mittleren Winter und bis 242 GW
in einer Kältewelle wie Januar 1997.** Sie wird von den Wärmepumpen bestimmt: Bei −14 °C im
Tagesmittel brauchen sie bis zu 130 GW, weil der Wärmebedarf hoch und die Leistungszahl
niedrig ist. Gesteuertes Laden der E-Autos senkt die Spitze nur um 15–20 GW, denn die
Kältespitze dauert Tage und lässt den Autos kaum Täler zum Ausweichen.

| Kennzahl | heute (Modell) | Vollausbau, ungesteuert | Vollausbau, gesteuert |
|---|---|---|---|
| Jahresbedarf (TWh, Mittel der Wetterjahre) | 468 | 755 | 755 |
| Jahreshöchstlast, Median der Wetterjahre (GW) | 73 | 185 | 168 |
| Jahreshöchstlast, 90. Perzentil (GW) | 75 | 215 | 196 |
| Jahreshöchstlast, kältestes Wetterjahr 1997 (GW) | 77 | 242 | 226 |

Die Spanne der Annahmen ist groß, ändert aber die Größenordnung nicht. Die Spitze im
Median liegt zwischen 168 und 195 GW (ungesteuert), im Extremjahr zwischen 213 und 259 GW
(Tabelle unten). Der stärkste Hebel ist der Wärmebedarf: 25 % weniger Raumwärme durch
Sanierung senkt die Extremspitze um 29 GW.

**Folgerung für die Prognose:** Für die Lastprognose zählt vor allem, dass die
Temperaturempfindlichkeit im Winter zunimmt. Ein Modell, das auf 2015–2020 gelernt hat,
unterschätzt kalte Tage heute um etwa 0,1 GW je Kelvin Heizgradtag, an einem Tag mit 20 K
also um 2 GW. Ein Trend in der Temperaturwirkung (oder kurze Trainingsfenster) ist deshalb
wichtiger als Zulassungszahlen als Merkmal. E-Autos verschieben die Last bisher nicht
messbar.

## Rückblick 2015–2025

![Geschätzter Verbrauch von E-Autos und Wärmepumpen](grafiken/rueckblick_verbrauch.png)

![Netzlast Deutschland](grafiken/rueckblick_last.png)

E-Autos: Bestand an batterieelektrischen Pkw und Plug-in-Hybriden aus Eurostat (31.12.),
Jahresmittel aus zwei Stichtagen, 12.309 km je Pkw und 20 kWh/100 km ab Netz; Plug-in-Hybride
fahren 40 % elektrisch. Wärmepumpen: Eurostat weist die Umweltwärme der Haushalts-
Wärmepumpen aus. Bei einer Jahresarbeitszahl von 3,2 kostet jede kWh Umweltwärme
1/2,2 kWh Strom. Für Gewerbe gibt es diese Reihe nicht. Die Absatzzahlen des BWP stehen zum
Vergleich in [tabellen/rueckblick_jahre.csv](tabellen/rueckblick_jahre.csv): 2,2 Mio.
Heizungswärmepumpen von 2007 bis 2025, davon 1,2 Mio. seit 2021.

![Temperaturempfindlichkeit der Tageslast](grafiken/temperaturempfindlichkeit.png)

Je Jahr eine Regression der Tageslast auf den Heizgradtag (Nov–Feb) bzw. den Kühlgradtag
(Jun–Aug), mit Wochentag, Feiertag, Weihnachten und Tageslänge als Kontrollgrößen.
Heizgradtag: 15 °C minus gedämpfte Tagesmitteltemperatur (Gewichte 1, ½, ¼, ⅛ der letzten
vier Tage). Kühlgradtag: Tagesmittel über 18 °C. Die Temperatur ist das einwohnergewichtete
Mittel von 22 DWD-Stationen. Die orange Linie ist der Anstieg, den der Wärmepumpenbestand der
Haushalte erwarten lässt, auf das Niveau 2015–2017 gesetzt. 2022 fällt nach oben heraus. Das war der Winter der Gaspreiskrise, vermutlich haben
viele mit Heizlüftern geheizt.

Klimageräte: Der FGK nennt über 330.000 verkaufte Split-Außengeräte 2020 und über 250.000
2024. Selbst eine Million zusätzlicher Geräte mit 300 kWh im Jahr wären 0,3 TWh. Das ist
in der Sommerlast nicht nachweisbar. Die Steigung je Kühlgradtag schwankt zwischen 0,03 und
0,32 GW und hat keinen Trend (−0,009 ± 0,006 GW je Jahr).

## Hochrechnung Vollausbau

![Jahreshöchstlast je Wetterjahr](grafiken/spitzen_wetterjahre.png)

![Kältewelle Januar 1997](grafiken/kaeltewelle.png)

![Mittlerer Winter](grafiken/typische_spitze.png)

Für jedes Wetterjahr 1991–2025 wird stündlich gerechnet:

1. **Heutige Last:** Stundenmodell der Netzlast 2023–2025 auf dem Niveau 2025 (Stunde ×
   Tagtyp, Monat, Heiz- und Kühlgradtag je Tageszeit, Weihnachten, Tageslänge; R² 0,94,
   mittlerer Fehler 2,3 GW). Es enthält die heutigen E-Autos und Wärmepumpen. Die
   Jahresspitze unterschätzt das Modell um 0–5 GW, seit 2020 meist um 3 GW
   ([tabellen/basismodell_spitzen.csv](tabellen/basismodell_spitzen.csv)).
2. **Wärmepumpen in Gebäuden:** Raumwärme und Warmwasser, die 2024 mit Gas, Öl oder Kohle
   erzeugt wurden (AGEB: Haushalte, GHD, Raumwärme der Industrie; 1.715 PJ), mal 0,9
   Kesselwirkungsgrad. Die Raumwärme wird auf die mittleren Heizgradtage der Wetterjahre
   umgerechnet (442 TWh Nutzwärme), Warmwasser bleibt konstant (68 TWh). Die Stundenwärme
   folgt dem Heizgradtag des Tages mit einem flachen Tagesgang. Die Leistungszahl hängt von
   der Stundentemperatur ab (Kurven aus when2heat, Ruhnau et al. 2019, mit 0,85 Praxisabschlag):
   85 % Luft-, 15 % Sole-Wärmepumpen, 75 % Heizkörper (Vorlauf 40 °C − Außentemperatur),
   25 % Flächenheizung, Warmwasser 50 °C.
3. **Wärmenetze:** Gebäude an Fernwärme bleiben dort. Vom fossilen Teil der Fernwärme
   (62 % von 133 TWh, BDEW) kommt die Hälfte aus Großwärmepumpen an Flüssen (Gütegrad 0,5,
   Vorlauf 70–95 °C), die andere Hälfte aus Abwärme, Geothermie, Biomasse oder Solarthermie
   ohne Strom.
4. **E-Autos:** 594 Mrd. km Pkw-Fahrleistung mit 20 kWh/100 km ab Netz, abzüglich der 5 %,
   die heute schon elektrisch sind (113 TWh). Im Winter bis 25 % mehr Verbrauch. Laden: 60 %
   zu Hause, 15 % am Arbeitsplatz, 10 % öffentlich tagsüber, 15 % Schnellladen. Ungesteuert
   lädt jedes Auto drei Stunden ab Ankunft. Gesteuert verteilt sich das Laden zu Hause auf
   17–7 Uhr und am Arbeitsplatz auf 8–17 Uhr so, dass die Gesamtlast möglichst flach bleibt.

Die Wärmepumpen bestimmen die Spitze. Im Januar 1997 sinkt das Tagesmittel auf −13,9 °C, die
Leistungszahl der Luft-Wärmepumpen an Heizkörpern auf etwa 2. In einem mittleren Winter
(Dezember 2001, −7 °C) liegt die Spitze bei 185 GW. Davon kommen 79 GW von Wärmepumpen und
36 GW von ungesteuert ladenden E-Autos um 18 Uhr. Gesteuert verschiebt sich das Laden in die
Nacht, die Spitze sinkt auf 168 GW.

Empfindlichkeit (Zusatzbedarf im Mittel der Wetterjahre, Spitzen in GW):

| Variante | Zusatzbedarf (TWh) | Spitze Median ungesteuert | Spitze Max ungesteuert | Spitze Median gesteuert | Spitze Max gesteuert | JAZ |
|---|---|---|---|---|---|---|
| Mitte | 287 | 185 | 242 | 168 | 226 | 3,2 |
| Sanierung −25 % Raumwärme | 253 | 168 | 213 | 148 | 194 | 3,2 |
| mehr Sole-WP (40 %) | 276 | 179 | 233 | 161 | 218 | 3,5 |
| mehr Flächenheizung (50 %) | 282 | 182 | 235 | 164 | 219 | 3,3 |
| COP ohne Praxisabschlag | 263 | 174 | 223 | 155 | 206 | 3,8 |
| COP-Abschlag 0,75 | 308 | 195 | 259 | 179 | 245 | 2,9 |
| E-Auto 17 kWh/100 km | 270 | 180 | 238 | 167 | 225 | 3,2 |
| E-Auto 24 kWh/100 km | 309 | 192 | 249 | 169 | 228 | 3,2 |
| Fernwärme ohne Großwärmepumpen | 270 | 179 | 233 | 162 | 217 | 3,2 |
| fossile Fernwärme ganz aus Großwärmepumpen | 304 | 191 | 251 | 174 | 236 | 3,2 |

## Grenzen

- Der Wärmebedarf wächst linear mit dem Heizgradtag, auch bei −14 °C. Echte Gebäude sind
  auf eine Normaußentemperatur von −12 bis −16 °C ausgelegt, die Annahme passt also bis in
  diesen Bereich. Heizstäbe in Wärmepumpen sind nicht abgebildet. Sie würden die Spitze an
  den kältesten Tagen weiter erhöhen.
- Die Wetterjahre 1991–2025 enthalten den Klimatrend. Künftige Winter werden im Mittel milder
  sein, Kältewellen wie 1997 oder 2012 bleiben aber möglich.
- Nicht enthalten: leichte Nutzfahrzeuge, Lkw und Busse, Prozesswärme der Industrie,
  Wasserstoff, Ausbau der Wärmenetze über den heutigen Stand, Wärmespeicher und
  Lastverschiebung bei Wärmepumpen. Ein Wegfall der Gas-KWK in Wärmenetzen ändert die
  Erzeugung, nicht die Last.
- Die heutige Last ist als fest angenommen. Effizienzgewinne bei Geräten und Industrie
  sind nicht fortgeschrieben.
- Die Netzlast von Energy-Charts enthält keinen Eigenverbrauch hinter dem Zähler. Wärmepumpen
  und Wallboxen, die PV-Strom vom eigenen Dach nutzen, fehlen im Rückblick zum Teil.

## Quellen

Frei, maschinell geladen:

- Netzlast stündlich 2015–2026: [Energy-Charts](https://api.energy-charts.info) `/public_power`
  (Fraunhofer ISE, CC BY 4.0)
- Lufttemperatur stündlich, 22 Stationen ab 1991:
  [DWD Open Data](https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/air_temperature/)
  (CC BY 4.0)
- Pkw-Bestand und Neuzulassungen nach Antrieb: Eurostat
  [road_eqs_carpda](https://ec.europa.eu/eurostat/databrowser/view/road_eqs_carpda/default/table),
  [road_eqr_carpda](https://ec.europa.eu/eurostat/databrowser/view/road_eqr_carpda/default/table)
  (CC BY 4.0)
- Endenergie der Haushalte nach Anwendung: Eurostat
  [nrg_d_hhq](https://ec.europa.eu/eurostat/databrowser/view/nrg_d_hhq/default/table) (CC BY 4.0)

Von Hand übernommen (nur für diese Analyse):

- Absatz Heizungswärmepumpen 2007–2025: [Bundesverband Wärmepumpe](https://www.waermepumpe.de/presse/zahlen-daten/absatzzahlen/)
- Absatz Split-Klimageräte 2020 und 2024: [FGK über haustechnikdialog.de](https://www.haustechnikdialog.de/News/30725/Marktentwicklung-Raumklimageraete-und-Luft-Luft-Waermepumpen)
- Raumwärme und Warmwasser nach Sektor und Energieträger 2024: [AGEB, Anwendungsbilanzen](https://ag-energiebilanzen.de/wp-content/uploads/EBD24e_AnwBil.pdf),
  Warmwasser der Haushalte 2023: [AGEB/RWI, Tab. 5](https://ag-energiebilanzen.de/wp-content/uploads/2024/11/AGEB_AnwBilanzen_2023_Endbericht_2025-04.pdf)
- Fernwärmeerzeugung 2025 nach Energieträgern: [BDEW Statusreport Wärme 2026, S. 56](https://www.bdew.de/media/documents/2026_07_10_Statusreport_Waerme_final_1.pdf)
- Pkw-Fahrleistung 2024 (KBA): [dpa über autohaus.de](https://www.autohaus.de/nachrichten/autohandel/trendwende-menschen-in-deutschland-fahren-wieder-mehr-auto-3688922)
- Pkw-Bestand 1.1.2026: [Destatis](https://www.destatis.de/DE/Presse/Pressemitteilungen/2026/07/PD26_N050_46_85.html)
- Ladeverluste 10–25 %: [ADAC über ecomento.de](https://ecomento.de/2020/07/23/adac-ermittelt-elektroauto-ladeverluste/)
- Leistungszahlkurven: Ruhnau, Hirth, Praktiknjo (2019), *Time series of heat demand and
  heat pump efficiency for energy system modeling*, Scientific Data 6:189

## Aufruf

Aus dem Repository-Wurzelverzeichnis. Der erste Lauf lädt DWD-, Energy-Charts- und
Eurostat-Daten (einige Minuten) in den Cache:

```bash
uv run --no-project --with pandas --with matplotlib --with httpx --with holidays \
    python docs/analysen/elektrifizierung-last/auswertung.py --cache /tmp/el
```

Ergebnisse: [tabellen/](tabellen/) (Jahreswerte, Temperaturempfindlichkeit, Wetterjahre,
Empfindlichkeit, `lauf.json` mit Kennzahlen) und [grafiken/](grafiken/).
