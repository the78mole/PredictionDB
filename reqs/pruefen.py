"""Prüft die Abdeckung der Softwareanforderungen durch Code und Tests.

StrictDoc bricht bei Verweisen auf unbekannte IDs ab, meldet aber nicht, wenn eine Anforderung
gar keinen Verweis hat. Dieses Skript ergänzt das für die SRS-Ebene:

- Jede umgesetzte SWR muss von mindestens einer Funktion oder Klasse außerhalb von ``tests/``
  referenziert werden.
- Jede umgesetzte SWR mit Nachweisart ``Test`` muss von mindestens einem Test referenziert
  werden.

Aufruf aus dem Repo-Wurzelverzeichnis: ``uv run python reqs/pruefen.py``. Rückgabewert 1, wenn
etwas fehlt.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQS = ROOT / "reqs"
SOURCES = ("packages", "services", "predict", "tests")

_BLOCK = re.compile(r"^\[REQUIREMENT\]\n(.*?)(?=^\[|\Z)", re.M | re.S)
_FIELD = re.compile(r"^(UID|STATUS|NACHWEIS): (.+)$", re.M)
_RELATION = re.compile(r"@relation\(([^)]*)\)")


@dataclass
class Requirement:
    uid: str
    status: str
    nachweis: set[str]
    document: Path


def read_requirements() -> list[Requirement]:
    out = []
    for path in sorted(REQS.rglob("*.sdoc")):
        for block in _BLOCK.finditer(path.read_text(encoding="utf-8")):
            fields = dict(_FIELD.findall(block.group(1)))
            out.append(
                Requirement(
                    uid=fields["UID"].strip(),
                    status=fields.get("STATUS", "").strip(),
                    nachweis={n.strip() for n in fields.get("NACHWEIS", "").split(",") if n},
                    document=path.relative_to(ROOT),
                )
            )
    return out


def read_markers() -> tuple[set[str], set[str]]:
    """IDs aus ``@relation``-Markern, getrennt nach Code und Tests."""
    code: set[str] = set()
    tests: set[str] = set()
    for top in SOURCES:
        for path in (ROOT / top).rglob("*.py"):
            if ".venv" in path.parts:
                continue
            target = tests if "tests" in path.relative_to(ROOT).parts else code
            for match in _RELATION.finditer(path.read_text(encoding="utf-8")):
                for part in match.group(1).split(","):
                    part = part.strip()
                    if part and "=" not in part:
                        target.add(part)
    return code, tests


def main() -> int:
    code, tests = read_markers()
    missing = []
    for req in read_requirements():
        if not req.uid.startswith("SWR-") or req.status != "Umgesetzt":
            continue
        if req.uid not in code:
            missing.append(f"{req.document}: {req.uid} ohne Umsetzung im Code")
        if "Test" in req.nachweis and req.uid not in tests:
            missing.append(f"{req.document}: {req.uid} ohne Test")
    for line in missing:
        print(line)
    if missing:
        print(f"{len(missing)} Lücke(n) in der Rückverfolgbarkeit", file=sys.stderr)
        return 1
    print("Rückverfolgbarkeit der SWR vollständig")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
