"""Freie Rohdaten für kkw-kuehlwasser-fr laden (nur lokal, nicht ins Repo).

- GFS-0p5-Silber von GHCR (CC BY 4.0, NOAA gemeinfrei): Läufe 00Z, f003 bis f024, je Standort
  Gitterpunkt und Einzugsgebiets-Kasten -> CACHE/gfs/<JJJJ-MM>.csv (Tagesmittel bzw. -summen)
- Hub'eau (Licence Ouverte 2.0): Stationen je Standort automatisch gewählt -> CACHE/hubeau/
  stationen.json, temp_<station>.csv (Stundenwerte), q_<site>.csv (Tagesmittel Abfluss, l/s)
- Energy-Charts (CC BY 4.0): /public_power fr, /cbet fr, /price FR und DE-LU je Jahr -> CACHE/ec/

Aufruf: uv run --no-project --with numpy --with numcodecs python -I \
    docs/analysen/kkw-kuehlwasser-fr/abruf.py --cache /tmp/kkw
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import sys
import tarfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numcodecs  # type: ignore[import-untyped]
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from daten import PEGEL_FEST, STANDORTE

REPO = "the78mole/predictiondb-data/gfs-0p5-silver"
EC = "https://api.energy-charts.info"
HUBEAU = "https://hubeau.eaufrance.fr/api"
START = dt.date(2021, 3, 1)
ENDE = dt.date(2026, 11, 1)
#: Größe -> (scale_factor, add_offset) wie in den zarr.json des Silbers
GROESSEN = {
    "t2m": (0.01, 273.15),
    "tp": (0.001, 30.0),
    "dswrf": (0.05, 0.0),
    "r2m": (0.01, 50.0),
    "soilw_0_10": (0.0001, 0.0),
    "weasd": (None, None),  # aus zarr.json gelesen
    "u10": (0.01, 0.0),
    "v10": (0.01, 0.0),
}
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
LAT = 71.5 - 0.5 * np.arange(75)
LON = -10.5 + 0.5 * np.arange(112)


def hole(url: str, token: str | None = None, accept: str | None = None) -> bytes:
    headers = {"User-Agent": "predictiondb-analyse"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    fehler: Exception | None = None
    for _ in range(6):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data: bytes = r.read()
                return data
        except Exception as exc:  # Netzfehler: bis zu 6 Versuche
            fehler = exc
    assert fehler is not None
    raise fehler


def jahre() -> range:
    return range(START.year, ENDE.year + 1)


def energy_charts(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    for jahr in jahre():
        zeit = f"start={jahr}-01-01T00:00Z&end={jahr}-12-31T23:59Z"
        for name, url in (
            (f"pp_fr_{jahr}.json", f"{EC}/public_power?country=fr&{zeit}"),
            (f"cbet_fr_{jahr}.json", f"{EC}/cbet?country=fr&{zeit}"),
            (f"price_fr_{jahr}.json", f"{EC}/price?bzn=FR&{zeit}"),
            (f"price_de_lu_{jahr}.json", f"{EC}/price?bzn=DE-LU&{zeit}"),
        ):
            if not (ziel / name).exists():
                (ziel / name).write_bytes(hole(url))


def _json(url: str) -> dict:  # type: ignore[type-arg]
    data: dict = json.loads(hole(url))  # type: ignore[type-arg]
    return data


def _fluss(name: str | None) -> str:
    return (name or "").lower().replace("le ", "").replace("la ", "").replace("l'", "").strip()


def stationen(ziel: Path) -> dict[str, dict[str, str | None]]:
    datei = ziel / "stationen.json"
    if datei.exists():
        alt: dict[str, dict[str, str | None]] = json.loads(datei.read_text())
        for code, (pegel, _) in PEGEL_FEST.items():
            alt[code]["abfluss"] = pegel
        return alt
    temp_alle = _json(f"{HUBEAU}/v1/temperature/station?size=5000")["data"]
    wahl: dict[str, dict[str, str | None]] = {}
    for s in STANDORTE:
        if s.gruppe == "kueste":
            continue
        fluss = _fluss(s.gewaesser)
        # Wassertemperatur: am selben Fluss, ≤ 1°, mit Daten ab 2021, nächste zuerst
        kand = [
            t
            for t in temp_alle
            if _fluss(t.get("libelle_cours_eau")) == fluss
            and abs(t["latitude"] - s.lat) <= 1
            and abs(t["longitude"] - s.lon) <= 1
        ]
        kand.sort(key=lambda t: math.hypot(t["latitude"] - s.lat, t["longitude"] - s.lon))
        t_code = None
        for t in kand:
            url = (
                f"{HUBEAU}/v1/temperature/chronique?code_station={t['code_station']}"
                f"&date_debut_mesure={START}&size=1&fields=date_mesure_temp"
            )
            if _json(url)["count"] > 1000:
                t_code = t["code_station"]
                break
        # Abfluss: Pegel am selben Fluss ≤ 0,6°, größtes Einzugsgebiet mit Tagesmitteln ab 2021
        bbox = f"{s.lon - 0.6},{s.lat - 0.6},{s.lon + 0.6},{s.lat + 0.6}"
        sites = _json(f"{HUBEAU}/v2/hydrometrie/referentiel/sites?bbox={bbox}&size=500")["data"]
        sites = [x for x in sites if _fluss(x.get("libelle_cours_eau")) == fluss]
        # nächster Pegel unter denen mit ≥ 20 % des größten Einzugsgebiets (Hauptstrom), damit
        # z. B. Bugey nicht den Rhône unterhalb der Saône-Mündung bekommt
        groesste = max((x.get("surface_bv") or 0 for x in sites), default=0)
        sites = [x for x in sites if (x.get("surface_bv") or 0) >= 0.2 * groesste > 0]
        sites.sort(
            key=lambda x: math.hypot(x["latitude_site"] - s.lat, x["longitude_site"] - s.lon)
        )
        q_code = None
        for x in sites[:8]:
            url = (
                f"{HUBEAU}/v2/hydrometrie/obs_elab?code_entite={x['code_site']}"
                f"&grandeur_hydro_elab=QmnJ&date_debut_obs_elab={START}&size=20"
            )
            if len(_json(url)["data"]) >= 20:
                q_code = x["code_site"]
                break
        wahl[s.code] = {"temperatur": t_code, "abfluss": q_code}
        print(s.code, wahl[s.code], flush=True)
    datei.write_text(json.dumps(wahl, indent=1))
    for code, (pegel, _) in PEGEL_FEST.items():
        wahl[code]["abfluss"] = pegel
    return wahl


def hubeau(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    wahl = stationen(ziel)
    for codes in wahl.values():
        t = codes["temperatur"]
        if t and not (ziel / f"temp_{t}.csv").exists():
            zeilen = ["datum,stunde,grad_c"]
            for jahr in jahre():
                for halb in (
                    (f"{jahr}-01-01", f"{jahr}-06-30"),
                    (f"{jahr}-07-01", f"{jahr}-12-31"),
                ):
                    url = (
                        f"{HUBEAU}/v1/temperature/chronique?code_station={t}"
                        f"&date_debut_mesure={halb[0]}&date_fin_mesure={halb[1]}&size=20000"
                        "&fields=date_mesure_temp,heure_mesure_temp,resultat"
                    )
                    for d in _json(url)["data"]:
                        zeilen.append(
                            f"{d['date_mesure_temp']},{d['heure_mesure_temp']},{d['resultat']}"
                        )
            (ziel / f"temp_{t}.csv").write_text("\n".join(zeilen) + "\n")
        q = codes["abfluss"]
        if q and not (ziel / f"q_{q}.csv").exists():
            zeilen = ["datum,l_s"]
            url: str | None = (
                f"{HUBEAU}/v2/hydrometrie/obs_elab?code_entite={q}&grandeur_hydro_elab=QmnJ"
                f"&date_debut_obs_elab={START}&date_fin_obs_elab={ENDE}&size=20000"
                "&fields=date_obs_elab,resultat_obs_elab"
            )
            while url:
                d = _json(url)
                zeilen += [f"{x['date_obs_elab']},{x['resultat_obs_elab']}" for x in d["data"]]
                url = d.get("next")
            (ziel / f"q_{q}.csv").write_text("\n".join(zeilen) + "\n")


def gfs(ziel: Path) -> None:
    ziel.mkdir(parents=True, exist_ok=True)
    token = json.loads(hole(f"https://ghcr.io/token?scope=repository:{REPO}:pull"))["token"]
    punkte = {s.code: (round((71.5 - s.lat) / 0.5), round((s.lon + 10.5) / 0.5)) for s in STANDORTE}
    kaesten = {}
    for s in STANDORTE:
        if s.einzugsgebiet:
            sued, nord, west, ost = s.einzugsgebiet
            kaesten[s.code] = (
                np.where((sued <= LAT) & (nord >= LAT))[0],
                np.where((west <= LON) & (ost >= LON))[0],
            )
    blosc = numcodecs.Blosc()
    monat = dt.date(START.year, START.month, 1)
    while monat < ENDE:
        datei = ziel / f"{monat:%Y-%m}.csv"
        naechster = (monat + dt.timedelta(32)).replace(day=1)
        if datei.exists():
            monat = naechster
            continue
        man = json.loads(
            hole(f"https://ghcr.io/v2/{REPO}/manifests/{monat:%Y-%m}-00z", token, MANIFEST)
        )
        layer = []
        for e in man["layers"]:
            lauf = dt.date.fromisoformat(e["annotations"]["org.predictiondb.run"][:10])
            if monat <= lauf < naechster:
                layer.append((lauf, e["digest"]))

        def eins(item: tuple[dt.date, str]) -> list[str]:
            tag, digest = item
            blob = hole(f"https://ghcr.io/v2/{REPO}/blobs/{digest}", token)
            werte = {}
            with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
                for g, (skala, versatz) in GROESSEN.items():
                    meta = tar.extractfile(f"{g}/zarr.json")
                    f = tar.extractfile(f"{g}/c/0/0/0")
                    assert meta is not None and f is not None
                    attr = json.loads(meta.read())["attributes"]
                    skala = attr.get("scale_factor", 1.0)
                    versatz = attr.get("add_offset", 0.0)
                    roh = np.frombuffer(blosc.decode(f.read()), "<i2").reshape(48, 75, 112)[1:9]
                    werte[g] = np.where(roh == -32768, np.nan, roh * skala + versatz)
            wind = np.hypot(werte["u10"], werte["v10"])
            zeilen = []
            for s in STANDORTE:
                i, j = punkte[s.code]
                t = werte["t2m"][:, i, j] - 273.15
                z = [
                    str(tag),
                    s.code,
                    "punkt",
                    f"{np.nanmean(t):.2f}",
                    f"{np.nanmax(t):.2f}",
                    f"{np.nansum(werte['tp'][:, i, j]) * 3:.2f}",
                    f"{np.nanmean(werte['dswrf'][:, i, j]):.1f}",
                    f"{np.nanmean(werte['r2m'][:, i, j]):.1f}",
                    f"{np.nanmean(werte['soilw_0_10'][:, i, j]):.4f}",
                    f"{np.nanmean(werte['weasd'][:, i, j]):.2f}",
                    f"{np.nanmean(wind[:, i, j]):.2f}",
                ]
                zeilen.append(",".join(z))
                if s.code in kaesten:
                    ii, jj = kaesten[s.code]
                    sub = {g: w[:, ii][:, :, jj] for g, w in werte.items()}
                    tk = sub["t2m"] - 273.15
                    z = [
                        str(tag),
                        s.code,
                        "einzugsgebiet",
                        f"{np.nanmean(tk):.2f}",
                        f"{np.nanmax(np.nanmean(tk, axis=(1, 2))):.2f}",
                        f"{np.nanmean(np.nansum(sub['tp'], axis=0)) * 3:.2f}",
                        f"{np.nanmean(sub['dswrf']):.1f}",
                        f"{np.nanmean(sub['r2m']):.1f}",
                        f"{np.nanmean(sub['soilw_0_10']):.4f}",
                        f"{np.nanmean(sub['weasd']):.2f}",
                        f"{np.nanmean(np.hypot(sub['u10'], sub['v10'])):.2f}",
                    ]
                    zeilen.append(",".join(z))
            return zeilen

        with ThreadPoolExecutor(12) as ex:
            ergebnis = list(ex.map(eins, sorted(layer)))
        kopf = "tag,standort,ort,t_mittel,t_max,niederschlag_mm,dswrf,rh,soilw,weasd,wind10"
        datei.write_text(kopf + "\n" + "\n".join(z for e in ergebnis for z in e) + "\n")
        print(f"GFS {monat:%Y-%m}: {len(layer)} Läufe", flush=True)
        monat = naechster


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--teil", choices=["ec", "hubeau", "gfs", "alle"], default="alle")
    args = p.parse_args()
    if args.teil in ("ec", "alle"):
        energy_charts(args.cache / "ec")
    if args.teil in ("hubeau", "alle"):
        hubeau(args.cache / "hubeau")
    if args.teil in ("gfs", "alle"):
        gfs(args.cache / "gfs")


if __name__ == "__main__":
    main()
