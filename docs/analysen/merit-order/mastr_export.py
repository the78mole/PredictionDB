"""MaStR-Tabellen für die Merit-Order-Analyse aus dem neuesten Bronze-Snapshot als CSV.

Läuft im Container ``mastr-snapshot`` des Stacks, weil dort Zugangsdaten und ``mastr_ingest``
bereitstehen::

    docker compose exec -T mastr-snapshot python - /tmp/meritorder \\
        < docs/analysen/merit-order/mastr_export.py
    docker compose cp mastr-snapshot:/tmp/meritorder ./meritorder-daten

Exportiert werden ``EinheitenVerbrennung`` (in Betrieb oder vorübergehend stillgelegt),
``AnlagenKwk``, ``EinheitenStromSpeicher`` (Pumpspeicher ab 1 MW) und ``EinheitenKernkraft``,
jeweils mit allen einfachen Feldern. Enum-Ids sind als Katalogtext aufgelöst. Das ZIP (gut
3 GB) wird danach wieder gelöscht.
"""

import csv
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import obstore
from lxml import etree

from mastr_ingest.units import read_catalog
from predictiondb_core.s3 import s3_store
from predictiondb_core.settings import get_settings

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/meritorder")
OUT.mkdir(parents=True, exist_ok=True)
info = []


def say(msg):
    print(msg, flush=True)
    info.append(msg)


settings = get_settings()
store = s3_store(settings.buckets.assets_bronze, settings)
keys = sorted(m["path"] for batch in obstore.list(store, prefix="mastr/") for m in batch)
manifests = sorted(k for k in keys if k.endswith("manifest.json"))
latest = manifests[-1]
manifest = json.loads(bytes(obstore.get(store, latest).bytes()))
say(f"Snapshot-Manifest: {latest}")
say(
    f"Exportdatum: {manifest.get('export_date')}  Release: {manifest.get('version')}  "
    f"Objekt: {manifest.get('object_key')}  Groesse: {manifest.get('size')}  "
    f"SHA256: {manifest.get('sha256')}  URL: {manifest.get('url')}"
)
say(f"Alle Manifeste im Bucket: {', '.join(manifests)}")

zpath = OUT / "snapshot.zip"
t = time.time()
with zpath.open("wb") as f:
    for chunk in obstore.get(store, manifest["object_key"]).stream():
        f.write(chunk)
say(f"ZIP geladen ({zpath.stat().st_size} Bytes, {time.time() - t:.0f}s)")

zf = zipfile.ZipFile(zpath)
catalog = read_catalog(zf)
say(f"Katalogwerte: {len(catalog)}")

NUMERIC = re.compile(
    r"leistung|kapazitaet|hoehe|anzahl|nummer|plz|postleitzahl|schluessel|datum|grad|jahr|"
    r"durchmesser|flaeche|laenge|breite|zeit|wirkungsgrad|nutzungsgrad|menge|id$|kennziffer|"
    r"hausnummer|nr$|code",
    re.I,
)
INT_LIST = re.compile(r"^\s*\d+(\s*,\s*\d+)*\s*$")


def iter_records(name):
    with zf.open(name) as f:
        depth = 0
        for ev, el in etree.iterparse(f, events=("start", "end"), recover=True, huge_tree=True):
            if ev == "start":
                depth += 1
                continue
            depth -= 1
            if depth == 1:
                rec = {c.tag: c.text for c in el if isinstance(c.tag, str) and c.text is not None}
                el.clear()
                parent = el.getparent()
                while el.getprevious() is not None:
                    del parent[0]
                yield rec


def members(table):
    pat = re.compile(rf"^{table}(?:_\d+)?\.xml$")
    out = [n for n in zf.namelist() if pat.match(n.rsplit("/", 1)[-1])]
    return sorted(
        out,
        key=lambda n: int(re.search(r"_(\d+)\.xml$", n)[1]) if "_" in n.rsplit("/", 1)[-1] else 0,
    )


def resolve(v):
    return ", ".join(catalog.get(p.strip(), p.strip()) for p in v.split(","))


def export(table, csv_name, keep=lambda rec, txt: True):
    files = members(table)
    say(f"{table}: Dateien {files}")
    rows, cols, total = [], {}, 0
    for name in files:
        for rec in iter_records(name):
            total += 1
            txt = lambda k, rec=rec: resolve(rec[k]) if rec.get(k) else ""  # noqa: E731
            if not keep(rec, txt):
                continue
            for k in rec:
                cols.setdefault(k, None)
            rows.append(rec)
    # Enum-Spalten: Name nicht numerisch, alle Werte Id-Listen aus dem Katalog
    enum_cols = []
    for c in cols:
        if NUMERIC.search(c):
            continue
        vals = [r[c] for r in rows if r.get(c)]
        if vals and all(
            INT_LIST.match(v) and all(p.strip() in catalog for p in v.split(",")) for v in vals
        ):
            enum_cols.append(c)
    with (OUT / csv_name).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(list(cols))
        for r in rows:
            w.writerow(
                [resolve(r[c]) if c in enum_cols and r.get(c) else (r.get(c) or "") for c in cols]
            )
    say(f"{csv_name}: {len(rows)} Zeilen von {total} Elementen, {len(cols)} Spalten")
    say(f"  aufgeloeste Enum-Spalten: {', '.join(enum_cols)}")


def num(v):
    try:
        return float((v or "").replace(",", "."))
    except ValueError:
        return 0.0


jobs = [
    (
        "EinheitenVerbrennung",
        "mastr_verbrennung.csv",
        lambda r, t: (
            t("EinheitBetriebsstatus").lower() in ("in betrieb", "vorübergehend stillgelegt")
        ),
    ),
    ("AnlagenKwk", "mastr_kwk.csv", lambda r, t: True),
    (
        "EinheitenStromSpeicher",
        "mastr_speicher.csv",
        lambda r, t: (
            "pumpspeicher" in t("Technologie").lower()
            and max(num(r.get("Bruttoleistung")), num(r.get("Nettonennleistung"))) >= 1000
        ),
    ),
    ("EinheitenKernkraft", "mastr_kernkraft.csv", lambda r, t: True),
]
for table, name, keep in jobs:
    try:
        export(table, name, keep)
    except Exception as e:
        say(f"FEHLER {table}: {e!r}")

zpath.unlink()
(OUT / "export_info_mastr.txt").write_text("\n".join(info) + "\n", encoding="utf-8")
sys.exit(0)
