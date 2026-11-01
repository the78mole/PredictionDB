# Gespeicherte Wetterdaten

Übersicht, welche Wetterdatenpunkte PredictionDB je Modell ablegt: Felder, Gebiet, Gitter,
Läufe und Vorlaufzeiten, jeweils für Bronze, Silber und Gold (Stufen siehe
[ARCHITEKTUR.md](ARCHITEKTUR.md) 5.1). Maßgeblich ist der Code; die Tabellen geben den Stand
der folgenden Dateien wieder und sind bei Änderungen dort mitzuziehen:

| Datensatz | Quelle (`meta.source`) | Download | Verarbeitung |
|---|---|---|---|
| GFS DE | `gfs_0p25` | `config/sources/gfs_0p25.yaml` | `config/process/gfs_0p25.yaml`, `services/wx_process` |
| GFS Europa | `gfs_0p5` | `config/sources/gfs_0p5.yaml` | `config/process/gfs_0p5.yaml`, `services/wx_process` |
| GEFS-Ensemble | `gefs_0p5` | `config/sources/gefs_0p5.yaml` | `services/wx_exchange` (`gefs.py`, `gefs_vm.py`) |

Silber-Variablen: `services/wx_process/src/wx_process/variables.py`; Gold-Features:
`services/wx_process/src/wx_process/features.py`; GEFS-Größen: `VARIABLES` und `STATS` in
`services/wx_exchange/src/wx_exchange/gefs.py` bzw. `gefs_vm.py`; Regionen:
`config/gefs/regions_v2.json.gz`. Weitere Modelle aus [ARCHITEKTUR.md](ARCHITEKTUR.md) 3.1
(ECMWF, ICON, …) sind noch nicht angebunden.

## 1. Überblick

| | GFS DE | GFS Europa | GEFS-Ensemble |
|---|---|---|---|
| Modell, Produkt | NOAA GFS v16, `pgrb2.0p25` | NOAA GFS v16, `pgrb2.0p50` | NOAA GEFS v12, `pgrb2a`/`pgrb2b` 0,5° |
| Gitter | 0,25° | 0,5° | 0,5° (dasselbe wie GFS Europa) |
| Ausschnitt | 44–58° N, 0–20° O (DE und Nachbarn) | 34,5–71,5° N, 10,5° W–45° O (Verbundnetz) | wie GFS Europa |
| Gitterpunkte | 57 × 81 = 4 617 | 75 × 112 = 8 400 | 75 × 112 = 8 400 (nur in Bronze) |
| Läufe | 00, 06, 12, 18 UTC | 00, 06, 12, 18 UTC | nur 00 UTC |
| Vorlauf | 0–120 h stündlich, 123–384 h dreistündlich (209 Schritte) | 0–384 h dreistündlich (129 Schritte) | 0–384 h sechsstündlich (65 Schritte) |
| Mitglieder | – | – | 31: Kontrolle `gec00`, `gep01`–`gep30` |
| Felder (GRIB) | 22 | 22 (wie GFS DE) | 8 |
| Historie ab | 2021-03-22 12 UTC (GFS v16 im AWS-Archiv) | 2021-03-22 12 UTC | 2021-03-22 12 UTC (`first_run`) |
| Zugang | NOMADS-Filter, dann AWS `noaa-gfs-bdp-pds`, Google `global-forecast-system` | nur AWS, Google | AWS `noaa-gefs-pds` |
| Silber | Gitterwerte je Lauf (Zarr) | Gitterwerte je Lauf (Zarr) | Werte je Mitglied und Region (kein Gitter) |
| Gold | Features je MaStR-Cluster | Features je Gebotszone | Mittel, Streuung, P10, P90 je Gebotszone |

## 2. GFS DE und GFS Europa

### 2.1 Bronze: geladene GRIB-Nachrichten

Beide Quellen laden dieselben 22 Felder (`messages:` in den Quell-Konfigurationen, Auswahl nach
[ARCHITEKTUR.md](ARCHITEKTUR.md) 4.3). Ohne Angabe von `fcst` werden alle passenden Nachrichten
übernommen, bei `TCDC` und `CSNOW` also Momentanwert und Intervallmittel.

| GRIB-Kürzel | Ebene | Bedeutung |
|---|---|---|
| `TMP` | 2 m above ground | Lufttemperatur |
| `DPT` | 2 m above ground | Taupunkt |
| `RH` | 2 m above ground | relative Feuchte |
| `UGRD`, `VGRD` | 10 m above ground | Windkomponenten 10 m |
| `UGRD`, `VGRD` | 100 m above ground | Windkomponenten 100 m |
| `GUST` | surface | Böen |
| `DSWRF` | surface | Globalstrahlung (kurzwellig, abwärts) |
| `TCDC` | entire atmosphere | Gesamtbedeckung |
| `APCP` | surface | Niederschlag (Summe) |
| `CSNOW` | surface | Schneefall (kategorisch) |
| `PRMSL` | mean sea level | Luftdruck auf Meereshöhe |
| `PRES` | surface | Luftdruck am Boden |
| `DLWRF` | surface | langwellige Gegenstrahlung |
| `SUNSD` | surface | Sonnenscheindauer |
| `WEASD` | surface | Schnee-Wasseräquivalent |
| `SNOD` | surface | Schneehöhe |
| `TSOIL` | 0-0.1 m, 0.1-0.4 m below ground | Bodentemperatur (zwei Schichten) |
| `SOILW` | 0-0.1 m, 0.1-0.4 m below ground | Bodenfeuchte (zwei Schichten) |

Die Felder ab `PRES` werden seit 2026-10-21 mitgeladen. Im Analyseschritt f000 fehlen
`DSWRF` und `APCP` (Manifest-Feld `absent`). Der NOMADS-Filter liefert das Kreuzprodukt aus
Variablen und Ebenen, Bronze von GFS DE kann daher einige zusätzliche Nachrichten enthalten
(z. B. `TMP` am Boden); Silber übernimmt nur die Nachrichten aus 2.2.

Ablage: `wx-bronze/gfs/0p25/JJJJ/MM/TT/HH/fSSS.grib2` bzw. `wx-bronze/gfs/0p5/…`, zugeschnitten
auf den Ausschnitt, Original-Codierung (NOMADS-Antworten verlustfrei in CCSDS umgepackt).
Bronze wird nach `GFS_BRONZE_DAYS` (Standard 92) gelöscht, sobald Silber vorliegt.

### 2.2 Silber: Gitterwerte je Lauf

Ein Zarr-v3-Store je Lauf unter `wx-silver/gfs/0p25/JJJJ/MM/TT/HH.zarr` bzw.
`wx-silver/gfs/0p5/…`, je Variable ein Array `(step, lat, lon)` über den ganzen Ausschnitt und
alle Schritte der Quelle (keine zeitliche Interpolation). Gespeichert als Int16 mit
`scale_factor`/`add_offset` (CF), Blosc/Zstd mit Shuffle; die Auflösung der Ablage steht in
der Spalte „Schritt“. Kopie in GHCR: `ghcr.io/the78mole/predictiondb-data/gfs-0p25-silver` bzw.
`gfs-0p5-silver` (siehe [ARCHITEKTUR.md](ARCHITEKTUR.md) 5.5).

| Variable | Einheit | Inhalt | aus | Schritt | Offset | Int16-Bereich | gespeichert |
|---|---|---|---|---|---|---|---|
| `t2m` | K | Temperatur 2 m | `TMP` 2 m | 0,01 | 273,15 | −54,52 … 600,82 | −54,52 … 600,82 |
| `d2m` | K | Taupunkt 2 m | `DPT` 2 m | 0,01 | 273,15 | −54,52 … 600,82 | −54,52 … 600,82 |
| `r2m` | % | relative Feuchte 2 m | `RH` 2 m | 0,01 | 50 | −277,67 … 377,67 | 0 … 377,67 |
| `u10`, `v10` | m s-1 | Wind 10 m (West-Ost, Süd-Nord) | `UGRD`/`VGRD` 10 m | 0,01 | 0 | ±327,67 | ±327,67 |
| `u100`, `v100` | m s-1 | Wind 100 m | `UGRD`/`VGRD` 100 m | 0,01 | 0 | ±327,67 | ±327,67 |
| `gust` | m s-1 | Böen am Boden | `GUST` | 0,01 | 0 | ±327,67 | 0 … 327,67 |
| `dswrf` | W m-2 | Globalstrahlung, Mittel im Intervall | `DSWRF` (Mittel) | 0,05 | 0 | ±1 638,35 | 0 … 1 638,35 |
| `tcc` | % | Gesamtbedeckung, Momentanwert | `TCDC` (Momentanwert) | 0,01 | 50 | −277,67 … 377,67 | 0 … 377,67 |
| `tcc_mean` | % | Gesamtbedeckung, Mittel im Intervall | `TCDC` (Mittel) | 0,01 | 50 | −277,67 … 377,67 | 0 … 377,67 |
| `tp` | kg m-2 h-1 | Niederschlag je Stunde im Intervall | `APCP` (Summe) | 0,001 | 30 | −2,767 … 62,767 | 0 … 62,767 |
| `csnow` | 1 | Schneefall, kategorisch (0/1) | `CSNOW` (Momentanwert) | 0,001 | 0 | ±32,767 | 0 … 32,767 |
| `csnow_mean` | 1 | Anteil Schneefall im Intervall | `CSNOW` (Mittel) | 0,001 | 0 | ±32,767 | 0 … 32,767 |
| `prmsl` | Pa | Luftdruck auf Meereshöhe | `PRMSL` | 1 | 100 000 | 67 233 … 132 767 | 67 233 … 132 767 |
| `sp` | Pa | Luftdruck am Boden | `PRES` surface | 1 | 87 500 | 54 733 … 120 267 | 54 733 … 120 267 |
| `dlwrf` | W m-2 | Gegenstrahlung, Mittel im Intervall | `DLWRF` (Mittel) | 0,05 | 0 | ±1 638,35 | 0 … 1 638,35 |
| `sunshine` | 1 | Sonnenscheinanteil im Intervall | `SUNSD` | 0,0001 | 0 | ±3,2767 | 0 … 3,2767 |
| `weasd` | kg m-2 | Schnee-Wasseräquivalent | `WEASD` | 0,1 | 0 | ±3 276,7 | 0 … 3 276,7 |
| `snod` | m | Schneehöhe | `SNOD` | 0,001 | 0 | ±32,767 | 0 … 32,767 |
| `tsoil_0_10`, `tsoil_10_40` | K | Bodentemperatur 0–10 cm, 10–40 cm | `TSOIL` | 0,01 | 273,15 | −54,52 … 600,82 | −54,52 … 600,82 |
| `soilw_0_10`, `soilw_10_40` | 1 | Bodenfeuchte (Volumenanteil) 0–10 cm, 10–40 cm | `SOILW` | 0,0001 | 0 | ±3,2767 | 0 … 3,2767 |

Gespeichert wird der Rohwert `round((x − Offset) / Schritt)` als vorzeichenbehaftetes Int16 im
Bereich −32 767 … 32 767; −32 768 ist der Füllwert (`_FillValue`, fehlender Wert). Zurück gilt
`x = Rohwert × Schritt + Offset`. Der Offset ist also der Wert beim Rohwert 0, die Mitte des
Int16-Bereichs, und dieser reicht von Offset − 32 767 × Schritt bis Offset + 32 767 × Schritt
(Spalte „Int16-Bereich“; bei `tp`: 30 ± 32,767 mm/h). Werte außerhalb werden beim Schreiben
auf die Grenze gesetzt (`np.clip` in `silver.encode`), nicht als fehlend markiert. Größen mit
Untergrenze 0 werden schon vorher auf 0 begrenzt (`min_value`, wegen Rundung beim Zurückrechnen
der Eimer); die Spalte „gespeichert“ zeigt den Bereich, der danach tatsächlich vorkommen kann. Bei
`tp` bleibt so der Teil −2,767 … 0 ungenutzt, der Offset liegt nicht ganz in der Mitte des
genutzten Bereichs. Der einzige Bereich, der bei extremem Wetter knapp werden
kann, ist `tp` mit höchstens 62,8 mm/h; bei stündlichen Schritten im 0,25°-Mittel ist das selten,
aber nicht ausgeschlossen. Alle anderen Grenzen liegen weit außerhalb des Physikalischen. Der
Rundungsfehler ist höchstens ein halber Schritt (z. B. 0,005 K bei `t2m`, 0,025 W m-2 bei `dswrf`).

Das sind 26 Variablen aus 22 Feldern (`TCDC` und `CSNOW` je zweifach, Windkomponenten getrennt).
Mittel und Summen bezieht GFS auf Eimer, die alle 6 h neu beginnen; Silber rechnet sie auf das
Intervall seit dem vorigen Schritt zurück (bis 120 h bei GFS DE eine Stunde, sonst drei
Stunden). `SUNSD` wird von GFS als Momentanwert gemeldet, summiert aber ebenfalls in
6-h-Eimern und steht in Silber als Anteil 0–1. In f000 sind alle Intervallgrößen leer (NaN).
Koordinaten je Store: `step` (Vorlauf in h), `lat`, `lon`; Gültigkeitszeit = Lauf + `step`.

### 2.3 Gold: Features in `feat.nwp_value`

Aus Silber werden je Region und Gültigkeitszeit Features gerechnet (Merkmale in
`meta.feature`, Aggregation `cluster_cap_weighted`, Stand in `meta.nwp_feature_run`).
Lineare Größen sind das gewichtete Mittel über die Zellen der Region; Wind in Nabenhöhe,
Leistungskurve und PV-Modell werden je Zelle gerechnet und dann gewichtet.

| Feature | Einheit | Berechnung | Art |
|---|---|---|---|
| `t2m` | degC | `t2m` | alle |
| `wind_speed_10m` | m s-1 | Betrag aus `u10`, `v10` | wind |
| `wind_speed_100m` | m s-1 | Betrag aus `u100`, `v100` | wind |
| `wind_speed_hub` | m s-1 | 100-m-Wind auf Nabenhöhe (Hellmann-Exponent aus 10 m und 100 m) | wind |
| `gust` | m s-1 | `gust` | wind |
| `wind_cf` | 1 | generische Leistungskurve (kubisch bis Nennwind) | wind |
| `wind_power` | MW | `wind_cf` × installierte Leistung des Clusters | wind, nur GFS DE |
| `ghi` | W m-2 | `dswrf` | pv |
| `cloud_cover` | % | `tcc` | pv |
| `snow_fraction` | 1 | `csnow_mean` | pv, hydro |
| `pv_cf` | 1 | PV-Modell aus `dswrf` und `t2m` (PR 0,85, −0,4 %/K, NOCT 45 °C) | pv |
| `pv_power` | MW | `pv_cf` × installierte Leistung des Clusters | pv, nur GFS DE |
| `precip` | mm h-1 | `tp` | hydro |

Gold ist **nicht quantisiert**: `feat.nwp_value.value` ist `real` (float32), die TimescaleDB-
Kompression nach 30 Tagen ist verlustfrei. Die Genauigkeit erbt Gold aber aus Silber, weil die
Features aus den schon quantisierten Silberwerten gerechnet werden.

Art `area` bekommt alle Features von `wind`, `pv` und `hydro`, Art `other` nur `t2m`. Die
übrigen Silber-Variablen (`d2m`, `r2m`, Druck, `dlwrf`, `sunshine`, Schnee, Boden) liegen
derzeit nur in Silber.

**Regionen GFS DE:** MaStR-Cluster aus `config/sources/mastr.yaml` (Gewichte je Zelle in
`assets.grid_weight`, Gitter `gfs_0p25`):

| Technologie | Cluster | Art |
|---|---|---|
| `wind_on` | 25 | wind (Nabenhöhe sonst 100 m, Kurve 3/12/25 m s-1) |
| `wind_off` | 4 | wind (sonst 105 m, Kurve 3,5/13/25 m s-1) |
| `pv_ground` | 20 | pv |
| `pv_roof` | 25 | pv |
| `hydro` | 8 | hydro |
| `biomass` | 8 | other |

**Regionen GFS Europa:** feste Regionen aus `config/gefs/regions_v2.json.gz` (Codes
`gefs:<code>` in `meta.region`, dieselben wie bei GEFS), ohne Anlagenbestand, also ohne
`wind_power` und `pv_power`. Liste in 3.3.

## 3. GEFS-Ensemble

### 3.1 Bronze: geladene GRIB-Nachrichten

Je Mitglied und Schritt aus den Dateien `pgrb2a`/`pgrb2b` (0,5°) per Byte-Bereich, auf den
Ausschnitt zugeschnitten (`fields:` in `config/sources/gefs_0p5.yaml`):

| Name | Produkt | GRIB-Kürzel | Ebene |
|---|---|---|---|
| `t2m` | a | `TMP` | 2 m above ground |
| `u10`, `v10` | a | `UGRD`, `VGRD` | 10 m above ground |
| `tp` | a | `APCP` | surface |
| `tcc` | a | `TCDC` | entire atmosphere |
| `dswrf` | a | `DSWRF` | surface |
| `u100`, `v100` | b | `UGRD`, `VGRD` | 100 m above ground |

Bronze (`wx-bronze/gefs/0p5/JJJJ/MM/TT/HH/<mitglied>.fSSS.grib2`) entsteht nur auf der VM für
den laufenden Monat und wird nach `GEFS_BRONZE_DAYS` (Standard 183) gelöscht, wenn das Silber
lokal und in GHCR gleich ist. Die Historie in GitHub Actions legt kein GRIB ab.

### 3.2 Silber: Werte je Mitglied, Schritt und Region

Je Lauf ein Tar (`wx-silver/gefs/0p5/JJJJ/MM/TT/HH.tar.gz`) mit `meta.json` und `values.npy`:
`float32`, Achsen Mitglied (31) × Schritt (65) × Region (53) × Größe (7). Kopie in GHCR:
`ghcr.io/the78mole/predictiondb-data/gefs-0p5-silver`, Tags `JJJJ-MM-00z`. Mittel und Summen
beziehen sich auf die 6 h vor dem Schritt; in f000 sind sie leer. Anders als bei GFS ist
GEFS-Silber nicht quantisiert (float32, NaN = unbekannt), also ohne feste Grenzen.

| Größe | Einheit | Berechnung |
|---|---|---|
| `t2m` | degC | `t2m` |
| `wind_speed_100m` | m s-1 | Betrag aus `u100`, `v100` |
| `wind_cf` | 1 | Leistungskurve auf Nabenhöhe (Hellmann-Exponent aus 10 m und 100 m; Nabenhöhe der Region, sonst wie `wind_on`) |
| `ghi` | W m-2 | `dswrf` |
| `pv_cf` | 1 | PV-Modell aus `dswrf` und `t2m` (wie GFS) |
| `cloud_cover` | % | `tcc` |
| `precip` | mm h-1 | `tp` / 6 h |

### 3.3 Regionen (GEFS und GFS Europa)

`regions_v2` auf dem 0,5°-Gitter, gebaut aus dem MaStR-Gesamtdatenexport (DL-DE BY 2.0) und
Natural Earth (gemeinfrei), 53 Regionen:

- **DE nach Anlagenbestand:** `DE:wind_on` (Nabenhöhe 119,8 m), `DE:wind_off` (108,3 m),
  `DE:pv`, dazu `DE:area`.
- **Gebotszonen bzw. Länder als Fläche (`area`):** AL, AT, BA, BE, BG, CH, CZ, DK1, DK2, EE,
  ES, FI, FR, GB, GR, HR, HU, IE, IT-Nord, IT-CNor, IT-CSud, IT-Sud, IT-Calabria, IT-Sicilia,
  IT-Sardegna, LT, LV, MD, ME, MK, NL, NO1–NO5, PL, PT, RO, RS, SE1–SE4, SI, SK, TR, UA, XK.

### 3.4 Gold: Ensemble-Kennzahlen

Je Region, Größe und Schritt über die Mitglieder: `mean`, `std`, `p10`, `p90` (nur wenn
mindestens 10 Mitglieder gültig sind). Merkmal `<größe>_<kennzahl>`, also 7 × 4 = 28 Features
wie `t2m_mean` oder `wind_cf_p90`, Aggregation `ensemble`, Region `gefs:<code>`, in
`feat.nwp_value` (float32, nicht quantisiert, wie 2.3).

## 4. Nicht im Betrieb: GFS vor v16 (GDEX)

`config/sources/gfs_0p25_gdex.yaml` beschreibt die Läufe vor GFS v16 (bis 2021-03-22 06 UTC)
aus NCAR GDEX d084001: gleiche Quelle `gfs_0p25`, gleicher Ausschnitt und dieselben 22 Felder,
aber nur 0–240 h dreistündlich und 252–384 h zwölfstündlich (93 Schritte). Diese Historie wird
seit 2026-10-29 nicht mehr aufgebaut; der Code bleibt vorerst im Repo.
