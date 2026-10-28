# Intraday-Preis aus freien Daten schätzen?

Einmalige Auswertung vom 28.10.2026. Frage: Lässt sich der Intraday-Preis DE-LU aus freien Daten
(Day-Ahead-Preis und Prognosefehler aus SMARD) annähernd schätzen? Eine freie Quelle für
Intraday-Preise gibt es nicht. Energy-Charts, SMARD und ENTSO-E liefern per API nur Day-Ahead,
und die EPEX-Intraday-Daten dürfen nicht weitergegeben werden.

Als Referenz dient der **ID-AEP** von Netztransparenz.de (`GET api/v1/data/IdAep`). Das ist ein
mengengewichteter Viertelstunden-Index über alle kontinuierlichen Intraday-Geschäfte (EPEX und
Nord Pool, Viertelstunden- und Stundenprodukte). Netztransparenz veröffentlicht keine Lizenz.
Die Rohdaten wurden deshalb nur für diese Auswertung abgerufen, nicht in die Datenbank
geschrieben und nicht veröffentlicht. Hier stehen nur aggregierte Kennzahlen.

Zeitraum: 1.7.2020 bis 27.10.2026, 221.671 Viertelstunden. Training bis Ende 2024, Test vom
1.1.2025 bis 27.10.2026.

## Ergebnis in Kürze

**Nur grob, und nur im Nachhinein.** Der Intraday-Preis folgt dem Day-Ahead-Preis im Mittel,
streut aber stark darum. Freie Prognosefehler erklären davon nur einen kleinen Teil.

- **Abstand zu Day-Ahead:** Im Median liegt der ID-AEP 19 EUR/MWh vom Day-Ahead-Preis weg, im
  Mittel 35 EUR/MWh. Im Tagesmittel (Base) sind es 17 EUR/MWh. 10 % der Viertelstunden liegen
  48 EUR/MWh oder mehr darunter, 10 % liegen 47 EUR/MWh oder mehr darüber.
- **Die Streuung kommt aus wenigen Spitzen:** Die größten 0,1 % der Viertelstunden machen 65 %
  der Varianz aus, die größten 1 % machen 80 % aus. Diese Spitzen lassen sich aus keinen freien
  Daten erklären.
- **Die Richtung stimmt meistens:** Wenn die Residuallast über ihrer Day-Ahead-Prognose liegt,
  liegt auch der Intraday-Preis über dem Day-Ahead-Preis. Ab einem Fehler von 3 GW stimmt die
  Richtung in 67 % der Viertelstunden. Im Mittel bringt 1 GW Prognosefehler 5 bis 9 EUR/MWh.
- **Der Gewinn durch freie Daten ist klein:** Ein robustes lineares Modell senkt den mittleren
  Fehler im Test von 32,1 auf 29,7 EUR/MWh (−8 %). Der Median sinkt von 18,7 auf 17,7 EUR/MWh.
  Auf gekappten Werten erklärt das Modell 17 % der Streuung, auf den Rohwerten nur 4 %.
- **Vorab gibt es nichts zu gewinnen:** Ohne Ist-Werte von Wind, PV und Last ist das beste Modell
  nicht besser als „Intraday = Day-Ahead“. Für die Prognose ist der Day-Ahead-Preis deshalb
  bereits die beste freie Schätzung des Intraday-Preises. Die Unsicherheit lässt sich nur als
  Band angeben.

## Abstand ID-AEP zu Day-Ahead je Jahr

Spread = ID-AEP minus Day-Ahead, in EUR/MWh je Viertelstunde. Bis September 2025 war der
Day-Ahead-Preis stündlich, ab Oktober 2025 viertelstündlich.

| Jahr | Day-Ahead-Mittel | ID-AEP-Mittel | MAE | P10 | P90 | Korrelation (Spearman) | MAE Tagesmittel |
|---|---|---|---|---|---|---|---|
| 2020 (ab Juli) | 37,4 | 39,1 | 12,9 | −16,9 | 19,5 | 0,61 | 6,0 |
| 2021 | 96,9 | 97,5 | 25,3 | −37,9 | 36,9 | 0,84 | 11,4 |
| 2022 | 235,5 | 235,0 | 58,8 | −89,8 | 87,0 | 0,84 | 28,6 |
| 2023 | 95,2 | 98,8 | 34,9 | −48,6 | 49,3 | 0,68 | 15,8 |
| 2024 | 78,5 | 83,8 | 36,2 | −43,1 | 42,4 | 0,69 | 20,1 |
| 2025 | 89,3 | 90,5 | 31,4 | −45,1 | 39,2 | 0,72 | 15,5 |
| 2026 (bis 27.10.) | 112,2 | 116,9 | 33,1 | −41,3 | 46,9 | 0,83 | 17,8 |
| gesamt | 111,7 | 114,0 | 34,8 | −48,2 | 47,1 | – | 17,3 |

Der ID-AEP liegt im Mittel 2 bis 5 EUR/MWh über dem Day-Ahead-Preis. Die Pearson-Korrelation ist
wegen der Spitzen deutlich kleiner als die Rangkorrelation (2024: 0,33 gegen 0,69).

## Spread je Prognosefehler der Residuallast

Fehler = (Ist-Last − Prognose) − (Ist-Wind − Prognose) − (Ist-PV − Prognose), in GW, alles aus
SMARD. Ein positiver Fehler bedeutet mehr Bedarf als am Vortag erwartet.

| Fehler (GW) | Viertelstunden | Mittel | Median | P10 | P90 |
|---|---|---|---|---|---|
| unter −6 | 8.412 | −27,5 | −22,5 | −85,5 | 23,7 |
| −6 bis −4 | 14.486 | −17,2 | −15,0 | −73,1 | 35,2 |
| −4 bis −2 | 30.931 | −11,2 | −10,1 | −63,8 | 37,5 |
| −2 bis −1 | 24.048 | −4,8 | −6,3 | −53,3 | 38,3 |
| −1 bis 0 | 28.531 | 0,5 | −3,8 | −46,9 | 41,5 |
| 0 bis 1 | 29.357 | 4,0 | −1,2 | −39,5 | 42,7 |
| 1 bis 2 | 26.046 | 7,0 | 0,8 | −36,5 | 48,9 |
| 2 bis 4 | 34.997 | 12,9 | 3,6 | −30,7 | 55,9 |
| 4 bis 6 | 15.392 | 24,0 | 7,7 | −24,2 | 65,1 |
| über 6 | 9.471 | 34,7 | 16,0 | −16,6 | 88,8 |

Der Zusammenhang ist eindeutig und monoton. Innerhalb jeder Klasse ist die Streuung aber größer
als der Unterschied zwischen den Klassen. Die Rangkorrelation liegt in jedem Jahr bei 0,25 bis
0,32. Ein Zeitversatz ist ausgeschlossen: Die Korrelation ist bei Versatz 0 am größten.

## Modelle im Test (1.1.2025 bis 27.10.2026)

| Modell | MAE | Median-Fehler | R² Spread |
|---|---|---|---|
| B0: Intraday = Day-Ahead | 32,1 | 18,7 | 0,00 |
| B1: Day-Ahead + mittleres Tagesprofil | 33,6 | – | −0,01 |
| G0: LightGBM nur mit vorab bekannten Merkmalen | 33,5 | – | −0,02 |
| L1: linear mit Prognosefehlern | 32,3 | – | 0,04 |
| G1: LightGBM mit Prognosefehlern | 31,6 | – | 0,03 |
| L2: Huber-Regression mit Prognosefehlern | 29,7 | 17,7 | 0,04 |
| L2 auf gekappten Werten (P1 bis P99) | 26,0 | 17,7 | 0,17 |
| B0 auf gekappten Werten | 28,4 | 18,7 | 0,00 |

Weitere Befunde:

- **Stundenmittel:** Auch auf Stundenbasis bleibt die Streuung groß (Korrelation 0,74, MAE 30,3).
  L2 senkt den Fehler von 28,5 auf 26,3 EUR/MWh.
- **Nur 15-min-Day-Ahead:** Für die Zeit seit Oktober 2025 wurde mit Daten bis April 2026
  trainiert und ab Mai 2026 getestet. L2 senkt den Fehler von 36,5 auf 34,2 EUR/MWh. Das Modell
  aus der Zeit vor Oktober 2025 schafft 34,9 EUR/MWh. Der Zusammenhang hat sich mit der
  Umstellung also kaum verändert. LightGBM, nur auf alten Daten trainiert, war 2026 schlechter als
  B0 (36,0 gegen 33,1 EUR/MWh).
- **Tageszeit:** Die größten Abweichungen gibt es abends von 18 bis 21 Uhr (MAE 49 bis 53
  EUR/MWh), die kleinsten nachts (18 bis 21 EUR/MWh).
- **reBAP:** Der Ausgleichsenergiepreis korreliert mit dem ID-AEP nur zu 0,65 (MAE 73,5 EUR/MWh).
  Er ist als Ersatz für den Intraday-Preis also noch schlechter.

## Einordnung

Ein freier „geschätzter Intraday-Preis“ als eigene Reihe lohnt sich nicht. Er wäre im Median nur
1 EUR/MWh besser als der Day-Ahead-Preis selbst, und er entsteht erst, wenn die Ist-Werte
vorliegen. Wenn Intraday für eine Anwendung zählt, zum Beispiel für Speicher-Arbitrage, kann man
den Day-Ahead-Preis mit einem Unsicherheitsband je Tageszeit nehmen. Die Bandbreite steht in den
Tabellen oben: P10 bis P90 etwa ±45 EUR/MWh je Viertelstunde, ±17 EUR/MWh im Tagesmittel.

## Nachrechnen

Die Daten bleiben lokal und kommen nicht ins Repo. `ntp_fetch.py` ruft ID-AEP und reBAP ab (im
Container `energy-ingest`, braucht Netztransparenz-Zugang). `db_export.sql` exportiert die
SMARD-Reihen aus `energy.value_best`. `auswertung.py` und `auswertung2.py` rechnen die Tabellen:

```sh
docker compose run --rm -T --no-deps --entrypoint python energy-ingest - 2020 < ntp_fetch.py > daten/ntp.csv
docker compose exec -T postgres sh -c 'psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db_export.sql | gzip > daten/energy_qh.csv.gz
uv run --no-project --with pandas --with numpy --with lightgbm --with scikit-learn python auswertung.py daten
uv run --no-project --with pandas --with numpy --with lightgbm --with scikit-learn python auswertung2.py daten
```
