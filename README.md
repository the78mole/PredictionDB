# PredictionDB

Strombedarfs- und Strompreisprognose für Deutschland auf Basis kostenfreier Wettermodelle
(NOAA GFS u. a.) und Energiedaten. Architektur: [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md), Prognosemodelle:
[docs/PROGNOSEMODELLE.md](docs/PROGNOSEMODELLE.md). Quellen mit
Schlüssel- oder Lizenzbedarf: [docs/DATENQUELLEN-ZUGANG.md](docs/DATENQUELLEN-ZUGANG.md).

## Aufbau

Monorepo als [uv](https://docs.astral.sh/uv/)-Workspace (Kapitel 6.1 der Architektur):

| Pfad | Inhalt |
|---|---|
| `packages/core` | `predictiondb_core`: Konfiguration (`PDB_*`), Logging, S3- und DB-Zugriff, Garage-Init |
| `packages/*`, `services/*` | weitere Bibliotheken und Dienste, je mit eigener `pyproject.toml` |
| `migrations/` | Alembic-Migrationen (SQL inkl. TimescaleDB-Befehle) |
| `compose.yaml` | Stack für die VM: TimescaleDB, Garage (Single Node), `garage-init`, `migrate` |
| `docker/Dockerfile` | ein Image für alle Python-Dienste |
| `deploy/systemd/`, `scripts/vm-update.sh` | Auto-Update des Stacks auf der VM |

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

## Beiträge

Beiträge setzen die Annahme der [Beitragsbedingungen](CONTRIBUTING.md) voraus,
insbesondere die Einräumung von Nutzungsrechten an den Projektinhaber.

## Lizenz

Business Source License 1.1, siehe [LICENSE](LICENSE).
