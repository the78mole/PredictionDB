"""Landing Page für GitHub Pages aus der Vorlage pages/index.html bauen.

Setzt die Zahl der Anforderungen (aus reqs/**/*.sdoc), die Kacheln der
Testberichte (aus den JUnit-XML unter <ziel>/tests) sowie Commit und Datum ein.
Aufruf: ``python pages/build_site.py build/pages``. Umgebungsvariablen
``COMMIT_SHA`` und ``CI_RUN_ID`` setzt der Workflow pages.yml.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from string import Template

WURZEL = Path(__file__).resolve().parent.parent
REPO = "https://github.com/the78mole/PredictionDB"
REPO_NAME = "the78mole/PredictionDB"
API = "https://api.github.com"

BERICHTE = (
    ("unit", "Unit-Tests", "Tests aller Pakete und Dienste ohne externe Abhängigkeiten."),
    (
        "integration",
        "Integrationstests",
        "Tests gegen den Compose-Stack mit TimescaleDB und Garage in der CI.",
    ),
)


@dataclass
class Ergebnis:
    tests: int
    fehler: int
    uebersprungen: int
    dauer: float


def junit_lesen(pfad: Path) -> Ergebnis | None:
    """Summen aus einer JUnit-XML von pytest, None wenn sie fehlt oder kaputt ist."""
    try:
        wurzel = ET.parse(pfad).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [wurzel] if wurzel.tag == "testsuite" else list(wurzel.iter("testsuite"))
    if not suites:
        return None

    def summe(attr: str) -> float:
        return sum(float(s.get(attr, 0) or 0) for s in suites)

    return Ergebnis(
        tests=int(summe("tests")),
        fehler=int(summe("failures") + summe("errors")),
        uebersprungen=int(summe("skipped")),
        dauer=summe("time"),
    )


def anforderungen_zaehlen(reqs: Path) -> int:
    return sum(
        zeile.strip() == "[REQUIREMENT]"
        for datei in reqs.rglob("*.sdoc")
        for zeile in datei.read_text(encoding="utf-8").splitlines()
    )


def testkachel(ziel: Path, name: str, titel: str, text: str) -> str:
    ergebnis = junit_lesen(ziel / "tests" / f"{name}.xml")
    if ergebnis is None or not (ziel / "tests" / f"{name}.html").exists():
        return (
            '<section class="card">'
            f'<h2>{titel}</h2><p class="stat warn">kein Bericht</p>'
            f"<p>{text} Für den letzten CI-Lauf liegt kein Bericht vor.</p></section>"
        )
    bestanden = ergebnis.tests - ergebnis.fehler - ergebnis.uebersprungen
    if ergebnis.fehler:
        stat = f'<p class="stat bad">{ergebnis.fehler} fehlgeschlagen</p>'
    else:
        stat = f'<p class="stat ok">{bestanden} bestanden</p>'
    details = f"{ergebnis.tests} Tests"
    if ergebnis.fehler:
        details += f", {bestanden} bestanden"
    if ergebnis.uebersprungen:
        details += f", {ergebnis.uebersprungen} übersprungen"
    details += f", {ergebnis.dauer:.0f} s"
    return (
        f'<a class="card" href="tests/{name}.html"><h2>{titel}</h2>{stat}'
        f"<p>{text} {details}.</p>"
        '<span class="more">Zum Bericht →</span></a>'
    )


def _api(pfad: str) -> dict[str, object] | None:
    """GET auf die GitHub-API, mit GH_TOKEN falls gesetzt; None bei jedem Fehler."""
    anfrage = urllib.request.Request(
        f"{API}{pfad}", headers={"Accept": "application/vnd.github+json"}
    )
    token = os.environ.get("GH_TOKEN", "")
    if token:
        anfrage.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(anfrage, timeout=10) as antwort:
            daten = json.load(antwort)
    except (OSError, ValueError):
        return None
    return daten if isinstance(daten, dict) else None


def github_zahlen() -> dict[str, str]:
    """Stars, offene Issues und offene PRs zum Bauzeitpunkt, ein Strich, wenn nicht abrufbar.

    Die Seite aktualisiert die Zahlen im Browser, das hier ist nur der Startwert
    für Besucher ohne JavaScript oder bei erschöpftem API-Kontingent.
    """
    zahlen = dict.fromkeys(("stars", "issues", "prs"), "&ndash;")
    repo = _api(f"/repos/{REPO_NAME}")
    if repo and isinstance(repo.get("stargazers_count"), int):
        zahlen["stars"] = str(repo["stargazers_count"])
    for schluessel, typ in (("issues", "issue"), ("prs", "pr")):
        q = urllib.parse.quote(f"repo:{REPO_NAME} type:{typ} state:open")
        treffer = _api(f"/search/issues?q={q}&per_page=1")
        if treffer and isinstance(treffer.get("total_count"), int):
            zahlen[schluessel] = str(treffer["total_count"])
    return zahlen


def assets_kopieren(ziel: Path) -> None:
    quelle = WURZEL / "pages" / "assets"
    if quelle.is_dir():
        shutil.copytree(quelle, ziel / "assets", dirs_exist_ok=True)


def commit_bestimmen() -> str:
    sha = os.environ.get("COMMIT_SHA", "")
    if sha:
        return sha
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=WURZEL,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unbekannt"


def bauen(ziel: Path) -> Path:
    ziel.mkdir(parents=True, exist_ok=True)
    vorlage = Template((WURZEL / "pages" / "index.html").read_text(encoding="utf-8"))
    commit = commit_bestimmen()
    run_id = os.environ.get("CI_RUN_ID", "")
    ci_link = (
        f' · <a href="{REPO}/actions/runs/{html.escape(run_id)}">CI-Lauf</a>' if run_id else ""
    )
    assets_kopieren(ziel)
    gh = github_zahlen()
    seite = vorlage.substitute(
        gh_stars=gh["stars"],
        gh_issues=gh["issues"],
        gh_prs=gh["prs"],
        req_count=anforderungen_zaehlen(WURZEL / "reqs"),
        test_cards="\n    ".join(testkachel(ziel, *b) for b in BERICHTE),
        built_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        commit=html.escape(commit),
        commit_short=html.escape(commit[:7]),
        ci_link=ci_link,
    )
    ausgabe = ziel / "index.html"
    ausgabe.write_text(seite, encoding="utf-8")
    (ziel / ".nojekyll").touch()
    return ausgabe


if __name__ == "__main__":
    print(bauen(Path(sys.argv[1] if len(sys.argv) > 1 else "build/pages")))
