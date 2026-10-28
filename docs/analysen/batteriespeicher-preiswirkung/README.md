# Preiswirkung von Batteriespeichern am Day-Ahead-Markt

Einmalige Auswertung vom 28.10.2026. Frage: Wie und ab wann verändern Batteriespeicher den
Börsenpreis DE-LU merklich? Der Speicherausbau ist hier nicht aus Prognosen der Bundesregierung
oder des Netzentwicklungsplans abgeleitet, sondern aus dem, was im Marktstammdatenregister
tatsächlich in Betrieb und in Planung steht. Die Preiswirkung kommt aus einer Simulation auf den
SMARD-Stunden des letzten Jahres.

## Ergebnis in Kürze

**Die Wirkung ist schon da und wird 2027 deutlich.** Ein zusätzlicher, am Markt geführter
Speicherpark von rund 2 GW (2 h) drückt die mittlere Tagesspanne des Day-Ahead-Preises im
Basisjahr (27.10.2025 bis 26.10.2026) um 10 %. Ab etwa 3 GW sinkt der Abendpreis um 5 €/MWh, der
Mittagspreis steigt um 5 €/MWh. Der marktgeführte Park ist allein in den zwölf Monaten des
Basisjahres von 2,4 auf 4,7 GW gewachsen. Läuft der Zubau weiter wie 2026, liegt die
Tagesspanne Ende 2027 um 14 %, Ende 2028 um 20 % unter dem Basisjahr. Kommen die im MaStR
gemeldeten Projekte termingerecht, sind es Ende 2027 schon 26 % und Ende 2028 33 %.

| Ende | marktgeführte GW | Tagesspanne | Abend 17–21 Uhr | Mittag 11–15 Uhr |
|---|---|---|---|---|
| 2026 | 5,1 | −6 % | −2 €/MWh | +2 €/MWh |
| 2027, Zubau wie 2026 | 7,5 | −14 % | −4 €/MWh | +4 €/MWh |
| 2027, MaStR-Planung termingerecht | 12,5 | −26 % | −8 €/MWh | +9 €/MWh |
| 2028, Zubau wie 2026 | 9,8 | −20 % | −6 €/MWh | +7 €/MWh |
| 2028, MaStR-Planung termingerecht | 17,0 | −33 % | −11 €/MWh | +13 €/MWh |
| 2030, Zubau wächst um 50 % je Jahr | 34,0 | −49 % | −17 €/MWh | +25 €/MWh |

Empirische Angebotskurve, erneuerbare Erzeugung wie im Basisjahr, 70 % der Leistung am
Day-Ahead-Preis wirksam (Rest Regelleistung, unvollständige Voraussicht). Die Merit-Order-Kurve
liefert fast dieselben Zahlen ([tabellen/zeitachse.csv](tabellen/zeitachse.csv)).

**Speicher glätten, sie senken das Preisniveau kaum.** Abends und in den Morgenspitzen sinkt der
Preis, mittags und nachts steigt er. Der mittlere Preis bleibt bis 10 GW Zubau auf ±0,4 €/MWh
gleich. Darüber steigt er sogar leicht, weil die Verluste (14 % bei Netz zu Netz) als zusätzliche
Last bezahlt werden müssen und der Mittagspreis stärker steigt, als der Abendpreis fällt.

**Mit dem EE-Zubau gerechnet bleibt die Spanne etwa auf heutigem Stand.** Die Tabelle oben hält
Wind und PV auf dem Stand des Basisjahres. Wächst PV weiter um 15,5 % und Wind um 6 % im Jahr und
regeln neue Anlagen bei negativen Preisen ab, wird die Tagesspanne ohne weitere Speicher bis 2030
um 9 % größer und der Mittagspreis fällt von 58 auf 20 €/MWh. Mit dem Speicherzubau liegt die
Spanne 2027 je nach Pfad 4 bis 13 % und 2030 9 bis 20 % unter dem Basisjahr. Speicher fangen den
PV-Zubau also mehr als auf, den Fall des Mittagspreises bremsen sie aber nur um 2 bis 6 €/MWh
(siehe [Abregelung und EE-Zubau](#abregelung-und-ee-zubau)).

**Negative Preise reagieren empfindlich auf die Abregelung.** Mit dem gemessenen Preis allein
reichen 1,3 GW Zubau für 25 % weniger Stunden mit negativem Preis, weil die meisten nur knapp
unter null liegen. Stehen in diesen Stunden aber 3 bis 6 GW abgeregelte Wind- und PV-Leistung
bereit, laden die Speicher zuerst diesen Strom und der Preis bleibt negativ. Dann braucht es 7 bis
14 GW. Auf Tagesspanne und Abendpreis hat die Abregelung kaum Einfluss. Im Markt sieht man
trotzdem schon eine Wende: Von Januar bis September 2026 gab es dreimal so viele Stunden mit
negativer Residuallast wie im Vorjahr (565 statt 185), aber weniger Stunden mit negativem Preis
(460 statt 525). Neben den Speichern wirkt hier das Solarspitzengesetz, die Zahlen trennen
beides nicht.

**Die Erlöse der Speicher fallen schnell.** Der reine Day-Ahead-Arbitrageerlös des Zubaus sinkt
von 82 €/kW/a bei 2 GW auf 53 bei 10 GW und 36 bei 20 GW. Mit mehr PV liegt er bei 20 GW noch bei
63 €/kW/a. Das bremst den Ausbau irgendwo zwischen 15 und 30 GW, wenn nicht Regelleistung,
Intraday und Netzentgeltbefreiung den Rest bezahlen.

**Für das Prognosemodell:** Die Wirkung ist so groß, dass das Modell sie schon 2027 als
systematischen Fehler zeigen wird, vor allem am Mittag zu niedrig und am Abend zu hoch. Ein
Vorschlag für Merkmale steht unten.

## Bestand und Ausbau

![Bestand](grafiken/bestand.png)

Batteriespeicher im MaStR am 28.10.2026 ([tabellen/bestand_jahr.csv](tabellen/bestand_jahr.csv)):

| Klasse | in Betrieb GW | GWh | Zubau 2025 GW | Zubau 2026 (Jan–Okt) GW | in Planung GW / GWh |
|---|---|---|---|---|---|
| Groß ≥ 1 MW | 4,51 | 8,04 | 0,82 | 1,93 | 12,9 / 37,7 |
| Gewerbe 30 kW–1 MW | 0,57 | 1,02 | 0,13 | 0,18 | 0,1 / 0,2 |
| Heim ≤ 30 kW | 15,6 | 25,1 | 2,6 | 3,0 | (nicht gemeldet) |
| zusammen | 20,7 | 34,1 | | | |

Zum Vergleich: Energy-Charts meldet aus demselben Register für Ende September 2026 21,5 GW und
33,7 GWh. Am aktuellen Rand fehlen Nachmeldungen, der Zubau 2026 ist also eher höher.

Die Großspeicher sind der Teil, der den Preis bewegt. Ihr Zubau hat sich 2026 mehr als
verdoppelt (1,9 GW in zehn Monaten nach 0,8 GW im ganzen Jahr 2025). Der Bestand hat im Mittel 1,8 h
Speicherdauer, die Planung 2,9 h. Die Simulation rechnet mit 2 h und unterschätzt damit die
künftigen Projekte etwas. Die gemeldeten Planungen
([tabellen/planung_mastr.csv](tabellen/planung_mastr.csv)) verteilen sich so: 1,7 GW bis Ende
2026, 6,1 GW 2027 (darin 1,2 GW mit überfälligem Termin), 4,5 GW 2028, danach fast nichts, weil
Projekte erst kurz vor dem Bau eingetragen werden.

Die Netzanschlussanfragen zeigen, wie viel dahinter wartet: 2025 gingen bei den Netzbetreibern
laut Bundesnetzagentur 18 158 Anfragen über rund 574 GW ein, zugesagt wurden gut 54 GW
([pv magazine, 25.08.2026](https://www.pv-magazine.de/2026/08/25/netzanschluesse-fuer-batteriespeicher-574-gigawatt-angefragt-54-gigawatt-zugesagt/),
nicht an der Originalquelle geprüft). Die Zusagen sind die realistische Obergrenze bis etwa 2030.
Der genehmigte Szenariorahmen zum Netzentwicklungsplan 2037 rechnete mit 24 GW Großspeichern
im Jahr 2037. Der Pfad mit wachsendem Zubau erreicht das 2029.

**Heimspeicher** sind gemessen an Leistung und Energie dreimal so groß wie die Großspeicher,
wirken aber kaum auf den Börsenpreis. Sie laden vormittags aus der eigenen PV-Anlage, oft schon
voll, bevor der Preis mittags am tiefsten ist, und entladen abends nach dem Hausverbrauch. In den
Ausbaupfaden zählen sie deshalb nicht mit (Anteil 0 %), Gewerbespeicher zu 30 %. Mit
dynamischen Tarifen und der Steuerung nach § 14a EnWG kann sich das ändern. 15 GW, die nur zu
einem Fünftel marktgeführt laufen, wären so viel wie der ganze heutige Großspeicherpark.

## Simulation

![Simulation](grafiken/simulation.png)

![Tagesprofil](grafiken/tagesprofil.png)

Im Sommer füllt der Zubau das Mittagstal und kappt die Abendspitze um 20 Uhr. Im Winter ist der
Tagesgang flacher, die Wirkung entsprechend kleiner.

![Preisdauerlinie](grafiken/preisdauerlinie.png)

Zubau mit 2 h Speicherdauer, Basisjahr 27.10.2025 bis 26.10.2026
([tabellen/simulation.csv](tabellen/simulation.csv)):

| Zubau GW | Tagesspanne €/MWh | Abend | Mittag | Mittel | Stunden < 0 | Stunden > 200 | Erlös €/kW/a |
|---|---|---|---|---|---|---|---|
| 0 | 143 | 147 | 58 | 109,0 | 462 | 503 | – |
| 2 | 127 | −3,3 | +3,1 | 108,8 | 289 | 460 | 82 |
| 5 | 111 | −7,0 | +7,6 | 108,9 | 201 | 420 | 67 |
| 10 | 94 | −11,2 | +14,0 | 109,4 | 127 | 383 | 52 |
| 20 | 74 | −16,1 | +24,3 | 110,5 | 74 | 301 | 36 |
| 40 | 53 | −21,2 | +36,9 | 112,2 | 66 | 233 | 18 |
| 10 (1 h) | 109 | −7,0 | +7,6 | 108,9 | 201 | 424 | 34 |
| 10 (4 h) | 82 | −15,5 | +23,4 | 110,3 | 74 | 310 | 72 |

Empirische Kurve. Die Speicherdauer zählt fast so viel wie die Leistung: 10 GW mit 1 h wirken
wie 5 GW mit 2 h.

Ab welchem Zubau (2 h) die Schwellen erreicht sind ([tabellen/schwellen.csv](tabellen/schwellen.csv)):

| Schwelle | EE wie heute | mit 50 % mehr PV, 25 % mehr Wind | 3–6 GW abgeregelt |
|---|---|---|
| Tagesspanne −10 % | 1,8–1,9 GW | 3,5 GW | 2,4–2,8 GW |
| Abendpreis −5 €/MWh | 3,0–3,4 GW | 4,4 GW | 3,3–3,6 GW |
| Mittagspreis +5 €/MWh | 3,2–3,3 GW | 8,1–11,7 GW | 4,3–6,0 GW |
| Stunden mit negativem Preis −25 % | 1,2–1,3 GW | nicht erreicht | 7,4–14,6 GW |

Spannen: empirische Kurve und Merit-Order-Kurve. Mit mehr Wind und PV regeln die neuen Anlagen
bei negativen Preisen ab, deshalb bleibt die Zahl der negativen Stunden dort gleich und sinkt
auch mit Speichern kaum.

![Zeitachse](grafiken/zeitachse.png)

## Abregelung und EE-Zubau

![Zeitachse mit EE-Zubau](grafiken/zeitachse_ee.png)

Jedes Jahr mit dem EE-Zubau bis dahin, einmal ohne weiteren Speicher und einmal mit dem
Speicherpfad, empirische Kurve ([tabellen/zeitachse_ee.csv](tabellen/zeitachse_ee.csv)):

| Ende | PV / Wind ggü. Basisjahr | Spanne ohne Speicherzubau | Spanne mit Zubau wie 2026 / wachsend / MaStR-Planung | Mittag ohne / mit | Abend ohne / mit |
|---|---|---|---|---|---|
| Basisjahr | – | 143 | – | 58 | 147 |
| 2027 | +27 % / +10 % | 153 | 138 / 134 / 125 | 39 / 41–43 | 136 / 129–132 |
| 2028 | +47 % / +17 % | 155 | 135 / 127 / 123 | 31 / 33–35 | 129 / 120–124 |
| 2030 | +96 % / +31 % | 156 | 131 / 115 / 128 | 20 / 22–25 | 115 / 100–107 |

Die Merit-Order-Kurve ist steiler: Ohne Speicher wächst die Spanne bis 2030 auf 165 €/MWh, mit
den Pfaden liegt sie bei 120 bis 138 €/MWh. Die Richtung ist dieselbe.

Drei Dinge folgen daraus:

- Der PV-Zubau drückt vor allem den Mittagspreis, weil die zusätzliche Einspeisung genau dort
  anfällt. Abgeregelte und neue, abregelnde Anlagen machen das Mittagstal breiter, aber nicht
  tiefer als null. Speicher heben es nur wenig, weil sie zuerst den Strom aufnehmen, der sonst
  abgeregelt würde.
- Sturmphasen mit viel Wind dauern oft länger als ein, zwei Tage. 2-h-Speicher verschieben
  Energie innerhalb des Tages und helfen dort wenig. Die Simulation rechnet je Woche und würde
  längere Verschiebungen erlauben, die Speicherdauer begrenzt sie aber.
- Für die Frage „ab wann merklich“ ändert sich wenig: Gegenüber dem Stand ohne weitere Speicher
  ist die Wirkung 2027 so groß wie in der Tabelle oben. Netto gegenüber heute fällt sie kleiner
  aus, weil der EE-Zubau die Spanne gleichzeitig vergrößert.

## Rückblick: Sieht man es schon?

Januar bis September je Jahr ([tabellen/rueckblick_ueberschuss.csv](tabellen/rueckblick_ueberschuss.csv)):

| Jahr | PV TWh | Stunden Residuallast < 0 | Stunden < 5 GW | Stunden Preis < 0 | davon negativ bei < 5 GW | Median Preis bei < 5 GW |
|---|---|---|---|---|---|---|
| 2023 | 50,1 | 31 | 136 | 188 | 68 % | −3,24 |
| 2024 | 57,0 | 66 | 312 | 413 | 75 % | −2,50 |
| 2025 | 66,4 | 185 | 489 | 525 | 76 % | −1,53 |
| 2026 | 76,2 | 565 | 893 | 460 | 49 % | 0,00 |

2026 ist der Bruch: Bei knapper Residuallast ist der Preis nur noch in der Hälfte der Stunden
negativ statt in drei Vierteln. Das passt zur Simulation, lässt sich aber nicht allein den
Speichern zuschreiben. Das Solarspitzengesetz (seit Februar 2025) und die Direktvermarkter
regeln PV bei negativen Preisen ab, was dieselbe Wirkung hat.

![Rückblick](grafiken/rueckblick.png)

Die Tagesspanne im Verhältnis zum Tagesmittel ist 2026 bei gleicher Spanne der Residuallast in
fast allen Klassen kleiner als 2024 und 2025
([tabellen/rueckblick_tagesspanne.csv](tabellen/rueckblick_tagesspanne.csv)). Weil 2026 das
Erdgas teuer war, steigt das Tagesmittel mit, die Kennzahl ist deshalb nur ein Hinweis.

## Modell

1. **Speicherbestand:** öffentliche Einheitenübersicht des MaStR (`mastr_abruf.py`). Einheiten
   über 30 kW vollständig mit Status, Inbetriebnahme, geplanter Inbetriebnahme und Stilllegung.
   Einheiten bis 30 kW sind 2,85 Millionen. Von ihnen zählt das Skript die Anzahl je
   Inbetriebnahmemonat und zieht je Monat eine Stichprobe von 500 Einheiten für die mittlere
   Leistung und Kapazität (2026: 5,5 kW und 8,7 kWh).
2. **Ausbaupfade** für den marktgeführten Park (Groß 100 %, Gewerbe 30 %, Heim 0 %):
   - *Zubau wie 2026:* der hochgerechnete Zubau 2026 (2,4 GW/a) jedes Jahr wieder,
   - *Zubau wächst:* derselbe Zubau, um 50 % je Jahr wachsend (2026 war es das 2,8-Fache von
     2025, der Faktor ist auf 1,5 gedeckelt),
   - *MaStR-Planung termingerecht:* Bestand plus alle gemeldeten Projekte zum geplanten Termin,
     überfällige im Jahr 2027.

   Wirksam am Day-Ahead-Preis ist der Zuwachs gegenüber dem Mittel des Basisjahres (3,5 GW),
   davon 70 %.
3. **Preis je Stunde als Funktion einer Zusatzlast x** (Laden positiv, Entladen negativ):
   `p_neu = p_gemessen + g(R + x) − g(R)` mit der Residuallast R (Last minus Wind und PV, SMARD).
   Zwei Kurven g:
   - *empirisch:* je Monat aus den Stunden des Monats und der Nachbarmonate, Preis über
     Residuallast monoton angepasst (isotone Regression). Sie enthält den Austausch mit den
     Nachbarländern. Streuung macht sie flacher, sie ist eher die untere Schranke.
   - *Merit-Order:* die Treppe aus [merit-order](../merit-order/README.md) für Winter, Ende
     Oktober und Sommer, über 1 GW geglättet, mit der dortigen Korrektur von +9,25 GW. Ohne
     Austausch ist sie steiler, eher die obere Schranke. Unterhalb der steuerbaren Treppe
     (Überschuss) gilt auch hier die empirische Kurve.

   ![Angebotskurven](grafiken/angebotskurven.png)

4. **Fahrplan:** Bei vollständigem Wettbewerb betreibt ein großer Speicherpark sich so, als
   minimiere er die Summe der Erzeugungskosten. Das ist ein lineares Programm je Woche (168 h,
   Zusatzlast in 40 Stufen je Richtung, Speicherstand am Wochenanfang halb voll und am Ende
   mindestens wieder halb voll, Wirkungsgrad 86 % Netz zu Netz). Es rechnet mit perfekter
   Voraussicht und überschätzt die Wirkung damit etwas. Deshalb zählen in den Ausbaupfaden nur
   70 % der Leistung.
5. **EE-Zubau:** zusätzliche Solar- und Windeinspeisung als Vielfaches der gemessenen, als
   negative Zusatzlast vor dem Speicher. Einmal als feste Variante (50 % mehr PV, 25 % mehr
   Wind), einmal je Jahr (PV +15,5 %/a wie die SMARD-Einspeisung Januar–September 2024 bis 2026,
   Wind +6 %/a nach Zubau, gerechnet ab der Mitte des Basisjahres). Neue Anlagen drücken den
   Preis nicht unter min(gemessen, 0), weil sie nach Solarspitzengesetz und § 51 EEG bei
   negativen Preisen abregeln. Die Kurve wird unter die gemessene Residuallast verlängert.
6. **Abregelung:** SMARD misst die Einspeisung nach der Abregelung. In Stunden mit Preis ≤ 0
   können 3 oder 6 GW abgeregelte Leistung (Sensitivität, frei veröffentlichte Mengen gibt es
   nicht) zusätzliche Last zum gemessenen Preis decken, erst darüber greift die Kurve.
   Netzbedingte Abregelung (Redispatch) wirkt nicht auf den Day-Ahead-Preis und ist nicht
   modelliert.

Nicht im Modell: Speicher in den Nachbarländern, Intraday- und Regelleistungsmärkte als eigene
Erlösquellen, Netzengpässe und netzdienliche Fahrweise, Pumpspeicher als eigener Akteur (sie
stecken mit ihrem heutigen Verhalten in den gemessenen Preisen), Rückwirkung auf
Kraftwerkseinsatz und Brennstoffpreise. Der Bestand des Basisjahres steckt in den gemessenen
Preisen, simuliert wird nur der Zubau darüber hinaus.

## Vorschlag: Speicher als Merkmal in `ml_train`

Nur ein Vorschlag, nicht umgesetzt.

1. **Bestand in die Datenbank.** `mastr_ingest` liest bisher Wind, Solar, Biomasse und Wasser.
   Dazu kämen `EinheitenStromSpeicher` und `AnlagenStromSpeicher` (nutzbare Kapazität) als
   Technologien `bess_gross`, `bess_gewerbe` und `bess_heim` in `assets.capacity_daily`, mit
   Leistung und Energie. Für die Historie reichen Inbetriebnahme- und Stilllegungsdatum, für die
   nahe Zukunft die gemeldeten Planungen. Die Merkmalsgruppe `capacity` bekäme damit
   automatisch `cap_bess_*`.
2. **Bestand allein reicht nicht.** LightGBM extrapoliert nicht: Was über dem höchsten
   Bestandswert der Trainingsdaten liegt, behandelt der Baum wie diesen Höchstwert. Bei einem
   Park, der sich jedes Jahr verdoppelt, lernt das Modell die Wirkung also immer zu spät.
3. **Besser: speicherbereinigte Residuallast.** Für jeden Prognosetag wird die vorhergesagte
   Residuallast aus `stage1` mit dem wirksamen Park (Leistung und Energie aus Punkt 1 mal
   Marktanteil) geglättet, ohne Preise, als Tal-Auffüllen und Spitzen-Kappen. Als Merkmale
   dienen die geglättete Residuallast und der Fahrplan x je Stunde. Das Modell kennt den
   Zusammenhang Preis über Residuallast aus allen Jahren, der Speicher verschiebt nur, wo es
   auf dieser Kurve landet. Damit wächst die Wirkung mit dem Bestand, ohne dass das Modell sie
   aus wenigen Jahren mit Speichern lernen muss.
4. **Alternative als Nachkorrektur:** die Prognose wie hier mit `p + g(R + x) − g(R)` und der
   empirischen Kurve der letzten Wochen korrigieren. Das braucht kein neues Training und lässt
   sich direkt im Backtest gegen das heutige Modell prüfen.
5. **Prüfen:** Backtest über 2025 und 2026, als sich der marktgeführte Park verdoppelt hat,
   getrennt nach Mittags- und Abendstunden. Erwartet wird, dass die Variante ohne Speicher
   2026 mittags zu tief und abends zu hoch liegt.

## Daten

- [Marktstammdatenregister](https://www.marktstammdatenregister.de), öffentliche
  Einheitenübersicht, Abruf 28.10.2026, Datenlizenz Deutschland – Namensnennung – 2.0. Der
  Gesamtdatenexport wäre vollständiger. Er lag aber auf `download.marktstammdatenregister.de`,
  das aus der Cloud-Umgebung nicht erreichbar war. Der Stack hat ihn im Bronze-Bucket, die
  Speichertabellen werden dort aber noch nicht ausgewertet.
- [SMARD](https://www.smard.de) (Bundesnetzagentur), Stundenwerte ab 2019 für Day-Ahead-Preis
  DE-LU, Netzlast, Residuallast und Erzeugung je Energieträger, CC BY 4.0 (`smard_abruf.py`).
- Merit-Order-Treppen aus [merit-order/tabellen](../merit-order/tabellen) (Stand 27.10.2026).
- Von Hand übernommen, nur zur Einordnung: Netzanschlussanfragen und -zusagen 2025 (pv
  magazine, s. o.), Bestand Ende September 2026 von
  [Energy-Charts](https://www.energy-charts.info), Szenariorahmen NEP 2037 (Genehmigung 2023).

## Nachrechnen

Rohdaten holen (MaStR rund 30 Minuten, SMARD wenige Minuten):

```bash
uv run --no-project --with requests python docs/analysen/batteriespeicher-preiswirkung/mastr_abruf.py ./speicher-daten/mastr
uv run --no-project --with requests python docs/analysen/batteriespeicher-preiswirkung/smard_abruf.py ./speicher-daten/smard
```

Auswerten (gut zwei Minuten, Tabellen und Grafiken landen in diesem Ordner):

```bash
uv run --no-project --with pandas --with numpy --with scipy --with matplotlib \
    python docs/analysen/batteriespeicher-preiswirkung/auswertung.py --daten ./speicher-daten
```

Annahmen (Basisjahr, Wirkungsgrad, Speicherdauer, Marktanteile, Schwellen) stehen oben in
`auswertung.py`. Die Stichprobe der Heimspeicher zieht zufällige Seiten, ein neuer Abruf
liefert deshalb leicht andere Mittelwerte.
