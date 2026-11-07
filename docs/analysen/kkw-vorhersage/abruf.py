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
    attr = json.loads(meta.read())["attributes"]
    blosc = numcodecs.Blosc()
    teile = []
    for c in (0, 1):
        f = tar.extractfile(f"{g}/c/{c}/0/0")
        assert f is not None
        teile.append(np.frombuffer(blosc.decode(f.read()), "<i2").reshape(CHUNK, 75, 112))
    roh = np.concatenate(teile)[SCHRITTE]
    return np.where(
        roh == -32768, np.nan, roh * attr.get("scale_factor", 1.0) + attr.get("add_offset", 0.0)
    )


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


def gfs(ziel: Path, nur: str | None = None) -> None:
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
        naechster = (monat + dt.timedelta(32)).replace(day=1)
        if datei.exists() or (nur and f"{monat:%Y-%m}" != nur):
            monat = naechster
            continue
        man = json.loads(
            hole(f"https://ghcr.io/v2/{REPO}/manifests/{monat:%Y-%m}-00z", token, MANIFEST)
        )
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
        print(f"GFS {monat:%Y-%m}: {len(layer)} Läufe", flush=True)
        monat = naechster


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--teil", choices=["hubeau", "gfs", "alle"], default="alle")
    p.add_argument("--monat", help="nur diesen Monat laden, JJJJ-MM (Probe)")
    a = p.parse_args()
    if a.teil in ("hubeau", "alle"):
        vorgaenger.hubeau(a.cache / "hubeau")
    if a.teil in ("gfs", "alle"):
        gfs(a.cache / "gfs", a.monat)


if __name__ == "__main__":
    main()
