# Betrieb auf PVE2

Stand: 2026-10-21, deployt von `main` (1e5755f).

## VM

| | |
|---|---|
| Proxmox | PVE2 (Cluster MolesCluster), VMID **108** `predictiondb`, Autostart an |
| Ressourcen | 8 vCPU (host), 24 GB RAM, Debian 13 (Cloud-Image) |
| Platten | `scsi0` 50 GB System, `scsi1` 500 GB ext4 `LABEL=pdb-data` auf `/var/lib/docker` (alle Volumes) |
| LAN (eth0, vmbr0) | **172.22.7.162** (DHCP, Default-Route) |
| Proxy-Netz (eth1, VNet `proxy`, VLAN 999) | **10.0.7.248** (DHCP, ohne Default-Route, siehe `/etc/netplan/90-proxy-no-default-route.yaml`) |
| SSH | `ssh daniel@172.22.7.162` (Key `~/.ssh/id_ed25519`, sudo ohne Passwort, Mitglied der Gruppe `docker`) |

Tipp: DHCP-Reservierungen für beide MACs anlegen (eth0 `bc:24:11:8c:27:98`, eth1 `bc:24:11:87:07:b6`),
damit sich die Adressen nicht ändern.

## Stack

Verzeichnis `/opt/predictiondb` (Git-Checkout von `main`, Remote `git@github.com:the78mole/PredictionDB.git`).

- `.env` (chmod 600) enthält alle Geheimnisse, zufällig erzeugt: Postgres-Passwort, Garage RPC-Secret
  und Admin-Token, S3-Schlüssel. Noch nicht in OpenBao; Zielbild siehe ARCHITEKTUR.md, Kapitel 8.3.
- `COMPOSE_FILE=compose.yaml:compose.vm.yaml` steht in der `.env`, daher reicht überall `docker compose ...`.
- `compose.vm.yaml` (nur auf der VM, per `.git/info/exclude` ignoriert) veröffentlicht die Ports.
- Image: lokal gebaut als `ghcr.io/the78mole/predictiondb:local` (`TAG=local`), weil die CI noch kein Image pusht.

| Dienst | Zustand | Erreichbar |
|---|---|---|
| `postgres` (TimescaleDB 2.31, PG 17) | läuft | `172.22.7.162:5432`, DB/User `predictiondb` |
| `garage` S3-API | läuft | `http://172.22.7.162:3900`, Region `garage` |
| `garage` Admin-API | läuft | nur `127.0.0.1:3903` auf der VM |
| `garage-init` | einmalig, fertig | Buckets `wx-bronze`, `wx-silver`, `ml-artifacts`, `backups` |
| `migrate` | einmalig, fertig | Schemas `meta`, `energy`, `feat`, `fcst` (Revision `0002_energy`) |
| `ingest-gfs` | läuft | prüft alle 600 s auf neue GFS-Läufe, schreibt nach `wx-bronze` |
| `wx-process` | läuft | rechnet vollständige GFS-Läufe zu Silber (Zarr) und Features je MaStR-Cluster |
| `mastr-snapshot` | läuft | ein MaStR-Export je Quartal (erster: 2026-10-20) |
| `grafana` | läuft | **https://predb.intern.chaintronics.com** (NPM-Proxy-Host 37 → `10.0.7.248:3000`), Benutzer `admin` |
| `energy-ingest` | läuft | alle 900 s SMARD und Energy-Charts nach `energy.*`, dazu Backfill; ENTSO-E aus, bis `PDB_ENTSOE_TOKEN` gesetzt ist |

Im Proxy-Netz ist nur Grafana veröffentlicht (`compose.vm.yaml`: `10.0.7.248:3000`). Der Proxy-Host im
NPM nutzt das Wildcard-Zertifikat `*.intern.chaintronics.com` und erzwingt HTTPS. Das Startdashboard
„PredictionDB Status“ zeigt Energiedaten und Backfill, verarbeitete GFS-Läufe, MaStR und Speicher.

## Zugangsdaten holen

```sh
ssh predb 'grep -E "^(POSTGRES_PASSWORD|PDB_S3_ACCESS_KEY_ID|PDB_S3_SECRET_ACCESS_KEY|GRAFANA_ADMIN_PASSWORD)=" /opt/predictiondb/.env'
```

## Häufige Befehle (auf der VM, in `/opt/predictiondb`)

```sh
docker compose ps -a                         # Status
docker compose logs -f ingest-gfs            # Logs live (JSON)
docker compose logs --since 1h postgres
docker compose exec postgres psql -U predictiondb -d predictiondb
docker compose exec garage /garage status
docker compose exec garage /garage bucket info wx-bronze
docker compose restart ingest-gfs
docker compose down                          # Stoppen (Volumes bleiben)
```

Vom Laptop aus:

```sh
psql "postgresql://predictiondb:<PASSWORT>@172.22.7.162:5432/predictiondb"
AWS_ACCESS_KEY_ID=<KEY> AWS_SECRET_ACCESS_KEY=<SECRET> \
  aws --endpoint-url http://172.22.7.162:3900 --region garage s3 ls s3://wx-bronze/ --recursive | head
```

## Aktualisieren

Das Repo ist privat; die VM hat noch keinen GitHub-Zugang. Neuen Stand vom Laptop übertragen:

```sh
git fetch origin && git bundle create /tmp/pdb.bundle origin/main
scp /tmp/pdb.bundle daniel@172.22.7.162:/tmp/
ssh daniel@172.22.7.162 'cd /opt/predictiondb && git fetch /tmp/pdb.bundle refs/remotes/origin/main:refs/remotes/origin/main \
  && git reset --hard origin/main \
  && docker build -f docker/Dockerfile -t ghcr.io/the78mole/predictiondb:local . \
  && docker compose run --rm garage-init && docker compose run --rm migrate && docker compose up -d'
```

Mit einem Read-only-Deploy-Key auf GitHub ginge das direkt per `git pull` auf der VM.
