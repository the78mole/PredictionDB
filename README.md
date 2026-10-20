# PredictionDB

Strombedarfs- und Strompreisprognose für Deutschland auf Basis kostenfreier Wettermodelle
(NOAA GFS u. a.) und Energiedaten. Architektur: [docs/ARCHITEKTUR.md](docs/ARCHITEKTUR.md).

## Aufbau

Monorepo als [uv](https://docs.astral.sh/uv/)-Workspace (Kapitel 6.1 der Architektur):

| Pfad | Inhalt |
|---|---|
| `packages/core` | `predictiondb_core`: Konfiguration (`PDB_*`), Logging, S3- und DB-Zugriff, Garage-Init |
| `packages/*`, `services/*` | weitere Bibliotheken und Dienste, je mit eigener `pyproject.toml` |
| `migrations/` | Alembic-Migrationen (SQL inkl. TimescaleDB-Befehle) |
| `compose.yaml` | Stack für die VM: TimescaleDB, Garage (Single Node), `garage-init`, `migrate` |
| `docker/Dockerfile` | ein Image für alle Python-Dienste |

## Lokal starten

```sh
cp .env.example .env          # Geheimnisse eintragen, siehe Kommentare
docker compose up -d --wait postgres garage
docker compose run --rm garage-init   # Layout, Schlüssel, Buckets (idempotent)
docker compose run --rm migrate       # alembic upgrade head
```

Auf der VM ohne Entwicklungs-Override: `docker compose -f compose.yaml ...`.

## Entwicklung

```sh
uv sync --all-packages
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                                   # Unit-Tests
PDB_INTEGRATION=1 uv run pytest tests/integration  # gegen den laufenden Stack
```

Neue Datenbankänderungen kommen als neue Alembic-Revision (`uv run alembic revision -m "..."`).

## Beiträge

Beiträge setzen die Annahme der [Beitragsbedingungen](CONTRIBUTING.md) voraus,
insbesondere die Einräumung von Nutzungsrechten an den Projektinhaber.

## Lizenz

Business Source License 1.1, siehe [LICENSE](LICENSE).
