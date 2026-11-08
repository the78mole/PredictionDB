"""Rohdaten für kkw-vorhersage laden (nur lokal, nicht ins Repo).

- GFS-0p5-Silber von GHCR (CC BY 4.0, NOAA gemeinfrei): Läufe 00Z, je Vorlauftag 0 bis 7
  Lufttemperatur und Strahlung am Gitterpunkt, Niederschlag, Boden und Schnee im Einzugsgebiet ->
  CACHE/gfs/<JJJJ-MM>.csv; die Läufe selbst bleiben nicht liegen
- Hub'eau (Licence Ouverte 2.0): wie ../kkw-kuehlwasser-fr/abruf.py -> CACHE/hubeau/

Aufruf: uv run --no-project --with numpy --with numcodecs python -I \
    docs/analysen/kkw-vorhersage/abruf.py --cache /tmp/kkwv
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numcodecs  # type: ignore[import-untyped]
import numpy as np

HIER = Path(__file__).resolve().parent
VORGAENGER = HIER.parent / "kkw-kuehlwasser-fr"
sys.path.insert(0, str(VORGAENGER))
from daten import STANDORTE  # noqa: E402

_spec = importlib.util.spec_from_file_location("kkw_abruf", VORGAENGER / "abruf.py")
assert _spec is not None and _spec.loader is not None
vorgaenger = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vorgaenger)
hole = vorgaenger.hole

REPO = vorgaenger.REPO
MANIFEST = vorgaenger.MANIFEST
START = dt.date(2021, 3, 1)
ENDE = dt.date(2026, 11, 1)
LAT = 71.5 - 0.5 * np.arange(75)
LON = -10.5 + 0.5 * np.arange(112)
GROESSEN = ("t2m", "tp", "dswrf", "soilw_0_10", "weasd")
#: Schritte je Chunk im Silber (``step_chunk``), Abstand 3 h
CHUNK = 48
#: Index des Schritts 3 h bis 192 h
SCHRITTE = np.arange(1, 65)
KOPF = "tag,vorlauf,standort,ort,t_mittel,niederschlag_mm,dswrf,soilw,weasd"


def _feld(tar: tarfile.TarFile, g: str) -> np.ndarray:
    meta = tar.extractfile(f"{g}/zarr.json")
    assert meta is not None
    kopf = json.loads(meta.read())
    attr = kopf["attributes"]
    # Datentyp und Füllwert wie im Array angegeben (Silber bisher int16 mit -32768)
    typ = np.dtype(kopf.get("data_type", "int16")).newbyteorder("<")
    fuell = kopf.get("fill_value", -32768)
    blosc = numcodecs.Blosc()
    teile = []
    for c in (0, 1):
        f = tar.extractfile(f"{g}/c/{c}/0/0")
        assert f is not None
        teile.append(np.frombuffer(blosc.decode(f.read()), typ).reshape(CHUNK, 75, 112))
    roh = np.concatenate(teile)[SCHRITTE].astype(np.float64)
    leer = np.isnan(roh) if fuell is None or fuell == "NaN" else roh == float(fuell)
    return np.where(leer, np.nan, roh * attr.get("scale_factor", 1.0) + attr.get("add_offset", 0.0))


def lauf(blob: bytes, tag: dt.date, punkte: dict, kaesten: dict) -> list[str]:  # type: ignore[type-arg]
    with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
        werte = {g: _feld(tar, g) for g in GROESSEN}
    zeilen = []
    for vorlauf in range(8):
        a = 8 * vorlauf
        w = {g: f[a : a + 8] for g, f in werte.items()}
        for s in STANDORTE:
            i, j = punkte[s.code]
            t = float(np.nanmean(w["t2m"][:, i, j])) - 273.15
            zeilen.append(
                f"{tag},{vorlauf},{s.code},punkt,{t:.2f},,{np.nanmean(w['dswrf'][:, i, j]):.1f},,"
            )
            if s.code in kaesten:
                ii, jj = kaesten[s.code]
                sub = {g: f[:, ii][:, :, jj] for g, f in w.items()}
                regen = float(np.nanmean(np.nansum(sub["tp"], axis=0))) * 3
                zeilen.append(
                    f"{tag},{vorlauf},{s.code},einzugsgebiet,,{regen:.2f},,"
                    f"{np.nanmean(sub['soilw_0_10']):.4f},{np.nanmean(sub['weasd']):.2f}"
                )
    return zeilen


def _manifest(token: str, monat: dt.date) -> tuple[dict, dict]:  # type: ignore[type-arg]
    """Manifest des Monats und seine Kennung (SHA-256, Layer-Digests) für ``lauf.json``."""
    roh = hole(f"https://ghcr.io/v2/{REPO}/manifests/{monat:%Y-%m}-00z", token, MANIFEST)
    man = json.loads(roh)
    kennung = {
        "tag": f"{monat:%Y-%m}-00z",
        "manifest_sha256": hashlib.sha256(roh).hexdigest(),
        "layers": sorted(e["digest"] for e in man["layers"]),
        "abgerufen": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    return man, kennung


def gfs(ziel: Path, nur: str | None = None, nur_manifeste: bool = False) -> None:
    """Monats-CSV je Monat; daneben ``<JJJJ-MM>.manifest.json`` mit den verwendeten Digests.

    ``nur_manifeste`` trägt für schon geladene Monate nur die Kennung nach (Stand des Abrufs).
    """
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
    monat = dt.date(START.year, START.month, 1)
    while monat < ENDE:
        datei = ziel / f"{monat:%Y-%m}.csv"
        kenn = ziel / f"{monat:%Y-%m}.manifest.json"
        naechster = (monat + dt.timedelta(32)).replace(day=1)
        if nur and f"{monat:%Y-%m}" != nur:
            monat = naechster
            continue
        if datei.exists():
            if nur_manifeste and not kenn.exists():
                _, kennung = _manifest(token, monat)
                kennung["nachgetragen"] = True
                kenn.write_text(json.dumps(kennung, indent=1) + "\n")
            monat = naechster
            continue
        if nur_manifeste:
            monat = naechster
            continue
        man, kennung = _manifest(token, monat)
        layer = []
        for e in man["layers"]:
            tag = dt.date.fromisoformat(e["annotations"]["org.predictiondb.run"][:10])
            if monat <= tag < naechster:
                layer.append((tag, e["digest"]))

        def eins(item: tuple[dt.date, str]) -> list[str]:
            tag, digest = item
            blob = hole(f"https://ghcr.io/v2/{REPO}/blobs/{digest}", token)
            return lauf(blob, tag, punkte, kaesten)

        with ThreadPoolExecutor(8) as ex:
            ergebnis = list(ex.map(eins, sorted(layer)))
        tmp = datei.with_suffix(".tmp")
        tmp.write_text(KOPF + "\n" + "\n".join(z for e in ergebnis for z in e) + "\n")
        tmp.rename(datei)
        kenn.write_text(json.dumps(kennung, indent=1) + "\n")
        print(f"GFS {monat:%Y-%m}: {len(layer)} Läufe", flush=True)
        monat = naechster


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--teil", choices=["hubeau", "gfs", "alle"], default="alle")
    p.add_argument("--monat", help="nur diesen Monat laden, JJJJ-MM (Probe)")
    p.add_argument(
        "--manifeste",
        action="store_true",
        help="für schon geladene Monate nur die GHCR-Kennung nachtragen",
    )
    a = p.parse_args()
    if a.teil in ("hubeau", "alle"):
        vorgaenger.hubeau(a.cache / "hubeau")
    if a.teil in ("gfs", "alle"):
        gfs(a.cache / "gfs", a.monat, a.manifeste)


if __name__ == "__main__":
    main()
