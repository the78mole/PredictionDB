# Silber: float16 statt Int16 mit Scale/Offset?

Einmalige Auswertung vom 01.11.2026. Frage: Wie viel größer oder kleiner wäre das GFS-Silber,
wenn die Gitterwerte als float16 statt als Int16 mit `scale_factor`/`add_offset` abgelegt
würden? Ein Neubau des bestehenden Silbers steht nicht zur Debatte. Es geht darum, ob sich ein
anderes Format für künftige Läufe lohnt.

## Ergebnis in Kürze

**float16 spart praktisch keinen Platz.** Über alle Variablen ist float16 nur 1–8 % kleiner.
Der Vorteil kommt ganz aus dem Luftdruck: Bei rund 1 000 hPa hat float16 nur einen Schritt von
0,5 hPa statt 1 Pa (0,01 hPa) heute, speichert also viel weniger Information. Ohne die beiden
Druckvariablen ist float16 gleich groß (aus Silber) bzw. 7–9 % größer (aus der GRIB-Quelle).

| Messung | Int16 (heute) | float16 | ohne `prmsl`/`sp` | float32 |
|---|---|---|---|---|
| Silber GFS DE, 5 Läufe | 90,1 MB | 82,9 MB (−8 %) | ±0 % | +68 % |
| Silber GFS Europa, 2 Läufe | 43,8 MB | 40,9 MB (−7 %) | ±0 % | +67 % |
| Quelle GFS DE, 01.01.2025 00Z, f000–f012 | 1,04 MB | 1,04 MB (−1 %) | +9 % | +36 % |
| Quelle GFS DE, 01.07.2025 00Z, f000–f012 | 1,07 MB | 1,04 MB (−3 %) | +7 % | +40 % |

Je Gruppe (Änderung der Größe float16 gegen Int16):

| Gruppe | Silber DE | Silber Europa | Quelle Januar | Quelle Juli | größter float16-Fehler |
|---|---|---|---|---|---|
| Temperaturen (`t2m`, `d2m`, `tsoil_*`) | ±0 % | +2 % | +18 % | −6 % | 0,012 K |
| Wind (`u10`, `v10`, `u100`, `v100`, `gust`) | +10 % | +10 % | +19 % | +34 % | 0,013 m/s |
| Strahlung (`dswrf`, `dlwrf`, `sunshine`) | −9 % | −10 % | −6 % | −12 % | 0,25 W/m² |
| Feuchte, Wolken, Bodenfeuchte | −10 % | −11 % | −14 % | −11 % | 0,03 % |
| Niederschlag, Schnee | +10 % | +6 % | +33 % | −32 % | 0,03 kg/m² |
| Druck (`prmsl`, `sp`, in hPa) | −64 % | −56 % | −62 % | −68 % | 50 Pa |

Warum das so ist: float16 hat 11 Bit Mantisse. Nahe 0 ist es feiner als die festen Schritte
von Int16 (Wind bei Flaute, Temperatur um 0 °C im Winter, wenig Schnee), und diese zusätzlichen,
verrauschten Bits komprimieren schlecht. Bei großen Beträgen ist es gröber (Strahlung, Druck,
Feuchte nahe 100 %) und komprimiert dann besser, aber nur, weil es weniger speichert. Die
Ersparnis bei Strahlung und Feuchte ist so groß wie der Genauigkeitsverlust dort (0,25 statt
0,025 W/m², 0,03 statt 0,005 %).

**Die Genauigkeit ist für die Prognose unkritisch**, in beiden Formaten liegt der Fehler weit
unter dem Modellfehler. float16 ist bei Wind und Temperatur sogar meist etwas genauer, bei
Strahlung, Feuchte und Druck gröber.

**Empfehlung:** Bei Int16 bleiben. float16 bringt keinen Platz, nur andere Grenzen (Maximum
65 504, Druck nur in hPa). Wo eine Grenze knapp ist (bisher nur `tp` bis 62,8 mm/h), reicht es,
Schritt und Offset dieser einen Variablen anzupassen.

## Methode

- **Silber:** Je Paket der erste Lauf eines Monats aus GHCR (`gfs-0p25-silver`: 2025-01,
  2025-04, 2025-07, 2025-10 je 00Z und 2026-10 12Z; `gfs-0p5-silver`: 2026-01, 2026-07 je 00Z),
  alle Schritte und Variablen. Die Int16-Rohwerte werden mit dem Kompressor des Silbers neu
  gepackt (Blosc/Zstd Stufe 5, Byte-Shuffle, Chunks zu 48 Schritten); das ergibt byte-genau
  die Größe im Store. float16 entsteht aus den schon quantisierten Werten, das schmeichelt
  float16 leicht (weniger Rauschen in den unteren Bits).
- **Quelle:** Für zwei Läufe die Schritte f000–f012 von GFS DE direkt aus dem AWS-Archiv, per
  `.idx`-Bereich geladen, zugeschnitten und dekodiert wie in `wx-process` (Intervallwerte), dann
  einmal mit `wx_process.silver.encode` als Int16 und einmal als float16 gepackt (ein Chunk
  über die 13 Schritte).
- **Einheiten für float16:** °C statt K, hPa statt Pa (101 300 Pa liegt über dem
  float16-Maximum 65 504), sonst wie Silber.
- Bitshuffle statt Byte-Shuffle ist für beide Formate schlechter (Int16 +12–19 %, float16
  +20–30 %, Vorlauf der Auswertung) und deshalb nicht in den Tabellen.

Tabellen: [tabellen/silber.csv](tabellen/silber.csv) (summiert je Gitter und Variable),
[tabellen/quelle.csv](tabellen/quelle.csv) (je Lauf und Variable). Skript:
[auswertung.py](auswertung.py).

```bash
uv run --package wx-process python docs/analysen/silber-float16/auswertung.py silber /tmp/sf16
uv run --package wx-process python docs/analysen/silber-float16/auswertung.py quelle /tmp/sf16
```

## Wo das Format steht

Jedes Array im Zarr-Store trägt sein Format selbst: `zarr.json` nennt `data_type` (`int16`),
`scale_factor`, `add_offset`, `_FillValue` und die Einheit (CF), `xarray.open_zarr` dekodiert
damit ohne den Code von PredictionDB. Die Gruppe und die Layer-Annotation in GHCR tragen
zusätzlich `silver_version`.

PredictionDB selbst liest diese Angaben aber nicht: `wx_process.silver.Silver.read` dekodiert
mit der fest codierten Tabelle `wx_process.variables.VARIABLES`, und der Import aus GHCR nimmt
nur Läufe mit der aktuellen `SILVER_VERSION` an. Jede Formatänderung bedeutet deshalb heute
einen Neubau. Würde `read` stattdessen Datentyp, Scale und Offset aus den Array-Attributen
nehmen, könnten alte und neue Läufe mit unterschiedlichem Format nebeneinander liegen; neue
Läufe bekämen das neue Format, alte blieben, wie sie sind.
