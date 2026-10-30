# PredictionDB

[![CI](https://github.com/the78mole/PredictionDB/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/the78mole/PredictionDB/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/the78mole/PredictionDB?filter=v*&sort=semver)](https://github.com/the78mole/PredictionDB/releases)
[![Lizenz: BSL 1.1](https://img.shields.io/badge/Lizenz-BSL%201.1-blue)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white)](.python-version)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](.pre-commit-config.yaml)
[![Renovate](https://img.shields.io/badge/Renovate-enabled-brightgreen?logo=renovatebot)](renovate.json)
![CodeRabbit Pull Request Reviews](https://img.shields.io/coderabbit/prs/github/the78mole/PredictionDB?utm_source=oss&utm_medium=github&utm_campaign=the78mole%2FPredictionDB&labelColor=171717&color=FF570A&link=https%3A%2F%2Fcoderabbit.ai&label=CodeRabbit+Reviews)

Strombedarfs- und Strompreisprognose für Deutschland auf Basis kostenfreier Wettermodelle
(NOAA GFS u. a.) und Energiedaten. Architektur: [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md), Prognosemodelle:
[docs/PROGNOSEMODELLE.md](docs/PROGNOSEMODELLE.md), Backtest:
[docs/BACKTEST.md](docs/BACKTEST.md). Quellen mit
Schlüssel- oder Lizenzbedarf: [docs/DATENQUELLEN-ZUGANG.md](docs/DATENQUELLEN-ZUGANG.md).

## Aufbau

Monorepo als [uv](https://docs.astral.sh/uv/)-Workspace (Kapitel 6.1 der Architektur):

| Pfad | Inhalt |
|---|---|
| `packages/core` | `predictiondb_core`: Konfiguration (`PDB_*`), Logging, S3- und DB-Zugriff, Garage-Init |
| `packages/gribkit` | GRIB2-Hilfen: `.idx`-Parser, Range-Merging, Zuschnitt |
| `services/*` | Dienste je mit eigener `pyproject.toml` und README: `wx_ingest`, `wx_process`, `wx_exchange`, `energy_ingest`, `mastr_ingest`, `ml_train` |
| `reqs/` | Anforderungen als StrictDoc-Projekt (StRS, SyRS, Systemarchitektur, SRS und SWA je Dienst), Verweise aus Code und Tests per `@relation` ([README](reqs/README.md)) |
| `migrations/` | Alembic-Migrationen (SQL inkl. TimescaleDB-Befehle) |
| `compose.yaml` | Stack für die VM: TimescaleDB, Garage (Single Node), Ingest (GFS, Energie, MaStR), `wx-process`, Grafana mit Prometheus; Profile `backfill`, `history` (GFS-Historie) und `ml` (Training, Inferenz), Übersicht in Kapitel 8.2 der Architektur |
| `docker/Dockerfile` | ein Image für alle Python-Dienste |
| `deploy/systemd/`, `scripts/vm-update.sh` | Auto-Update des Stacks auf der VM |
| `predict/` | schlanker Prognose-Stack für Nutzer der Prognosen: ein Container mit Modellpaket, Eingänge direkt von freien Quellen, Ausgabe als Dateien und HTTP, eigene `compose.yaml` ([README](predict/README.md)) |

## Lokal starten

```sh
cp .env.example .env          # Geheimnisse eintragen, siehe Kommentare
docker compose up -d --wait postgres garage
docker compose run --rm garage-init   # Layout, Schlüssel, Buckets (idempotent)
docker compose run --rm migrate       # alembic upgrade head
```

Auf der VM ohne Entwicklungs-Override: `docker compose -f compose.yaml ...`.

## Auto-Update des Stacks

Nach jeder grünen CI auf `main` baut der Workflow „Image“ das App-Image und legt es in GHCR ab
(`ghcr.io/the78mole/predictiondb:<commit-sha>` und `:latest`). Auf der VM prüft ein systemd-Timer
alle 15 Minuten, ob `main` einen neuen Commit hat. Ist das Image dazu schon gebaut, rollt
`scripts/vm-update.sh` ihn aus: Checkout auf den Commit, Image ziehen und lokal als `deployed`
markieren, `garage-init`, `migrate`, `docker compose up -d`. Auf der VM wird nichts gebaut. Schlägt
ein Schritt fehl, bleibt der alte Stand laufen und der nächste Lauf versucht es erneut; der zuletzt
ausgerollte Commit steht in `.git/pdb-deployed`.

Einrichtung auf der VM (Checkout unter `/opt/predictiondb`, Benutzer in der Gruppe `docker`):

```sh
# in der .env: TAG=deployed
sudo cp deploy/systemd/predictiondb-update@.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now predictiondb-update@<benutzer>.timer
```

Bedienung: `systemctl list-timers 'predictiondb-update*'`, Protokoll mit
`journalctl -u 'predictiondb-update@*'`, sofort ausrollen mit
`sudo systemctl start predictiondb-update@<benutzer>.service` oder von Hand
`scripts/vm-update.sh --force`.

## Entwicklung

```sh
uv sync --all-packages
uv run pre-commit install                       # einmalig: Prüfungen vor jedem Commit
uv run pre-commit run --all-files               # Ruff (check, format) und mypy, wie die CI
uv run pytest                                   # Unit-Tests
PDB_INTEGRATION=1 uv run pytest tests/integration  # gegen den laufenden Stack
```

Die Hooks in `.pre-commit-config.yaml` rufen Ruff und mypy über `uv run` mit den Versionen aus
`uv.lock` auf; der CI-Job „Lint und Typen“ führt genau diese Hooks aus. `ruff format` korrigiert
die Formatierung dabei selbst, die geänderten Dateien müssen danach nur neu gestaged werden.

Neue Datenbankänderungen kommen als neue Alembic-Revision (`uv run alembic revision -m "..."`).

## Versionen und Releases

Nach jeder grünen CI auf `main` legt der Workflow „Release“ ein Tag `vX.Y.Z` mit GitHub-Release an.
Die Stufe ergibt sich aus den Commits seit dem letzten Tag nach
[Conventional Commits](https://www.conventionalcommits.org/de/) (bei Squash-Merges zählt der
PR-Titel): `feat:` hebt die Minor-Stelle, `typ!:` oder `BREAKING CHANGE:` die Major-Stelle
(solange 0.x: Minor), alles andere die Patch-Stelle (`scripts/naechste-version.sh`).

An jedes Release hängt der Workflow „Analysen als PDF“ die Analysen aus `docs/analysen/`, gerendert
von `scripts/analysen-pdf.sh` (Pandoc und Chrome/Chromium). Neu gerendert werden nur Analysen,
deren Ordner sich seit dem vorherigen Release geändert hat (oder alle, wenn sich der Renderer
geändert hat, `scripts/analysen-pdf-auswahl.sh`); die übrigen PDFs werden aus dem vorherigen
Release übernommen, sodass jedes Release alle PDFs trägt. Bei PRs, die Analysen ändern, liegen die
geänderten PDFs als Artefakt `analysen-pdf` bereit.

Der Prognose-Stack `predict/` hat eine eigene Reihe `predict/vX.Y.Z` (Workflow „Predict“) mit
dem Image `ghcr.io/the78mole/predictiondb-predict`. Sie zählt nur Commits, die den Stack oder die
von ihm genutzten Pakete ändern, und berührt die Tags `vX.Y.Z` nicht (`naechste-version.sh
--prefix predict/ -- <pfade>`). Ihre Releases tragen keine Analyse-PDFs.

## Beiträge

Beiträge setzen die Annahme der [Beitragsbedingungen](CONTRIBUTING.md) voraus,
insbesondere die Einräumung von Nutzungsrechten an den Projektinhaber.

## Lizenz

Business Source License 1.1, siehe [LICENSE](LICENSE).
