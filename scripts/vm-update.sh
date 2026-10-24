#!/usr/bin/env bash
# Auto-Update des Stacks: holt main von GitHub und rollt den neuen Stand mit dem
# fertigen Image aus GHCR aus (Image ziehen, garage-init, migrate, compose up).
# Gebaut wird hier nichts; das Image baut der Workflow "Image" nach grüner CI.
# Läuft per systemd-Timer (deploy/systemd/), kann aber auch von Hand laufen.
#
#   scripts/vm-update.sh [--force]
#     --force   auch ohne neuen Commit ausrollen
#
# Das ausgerollte Image bekommt lokal den Tag "deployed"; in der .env der VM steht
# TAG=deployed, damit auch Compose-Befehle von Hand diesen Stand verwenden.
# Der zuletzt erfolgreich ausgerollte Commit steht in .git/pdb-deployed. Schlägt
# ein Schritt fehl oder ist das Image zum Commit noch nicht gebaut, bleibt er
# unverändert und der nächste Lauf versucht es erneut.
#
# Umgebung: PDB_UPDATE_REPO (Vorgabe: öffentliches GitHub-Repo per HTTPS),
#           PDB_UPDATE_BRANCH (Vorgabe: main),
#           PDB_IMAGE (Vorgabe: ghcr.io/the78mole/predictiondb).
set -euo pipefail

# Alles in Funktionen: reset --hard kann dieses Skript während des Laufs
# ersetzen, Bash hat die Funktionen dann aber schon vollständig gelesen.
update() {
  local force=$1 neu alt
  git fetch --quiet "$repo" "+refs/heads/$branch:refs/remotes/origin/$branch"
  neu=$(git rev-parse "origin/$branch")
  alt=$(cat .git/pdb-deployed 2>/dev/null || true)
  if [[ $neu == "$alt" && $force != 1 ]]; then
    return 0
  fi

  # Das Image erscheint erst, wenn CI und Image-Workflow durch sind
  if ! docker pull --quiet "$image:$neu" >/dev/null 2>&1; then
    echo "Image $image:${neu:0:7} noch nicht verfügbar, nächster Versuch beim nächsten Lauf"
    return 0
  fi

  echo "Rolle ${neu:0:7} aus (bisher ${alt:0:7})"
  git log --oneline "${alt:-$neu~1}..$neu" 2>/dev/null | head -20 || true
  # Untracked Dateien (.env, compose.vm.yaml) bleiben bei reset --hard erhalten
  git reset --hard --quiet "$neu"
  docker tag "$image:$neu" "$image:deployed"
  docker compose run --rm garage-init
  docker compose run --rm migrate
  docker compose up -d
  # Ältere Commit-Tags des App-Images entfernen (je ~1 GB unter /var/lib/containerd auf /);
  # "deployed" zeigt auf denselben Stand, laufende Container bleiben unberührt
  docker image ls --format '{{.Repository}}:{{.Tag}}' "$image" \
    | grep -vxF -e "$image:$neu" -e "$image:deployed" \
    | xargs -r docker rmi >/dev/null 2>&1 || true
  docker image prune -f >/dev/null || true
  echo "$neu" > .git/pdb-deployed
  echo "Ausgerollt: $(git log -1 --format='%h %s')"
}

main() {
  local force=0
  while [[ $# -gt 0 ]]; do
    case $1 in
      --force) force=1 ;;
      *) echo "unbekannte Option $1" >&2; exit 2 ;;
    esac
    shift
  done

  root=$(cd "$(dirname "$0")/.." && pwd)
  repo=${PDB_UPDATE_REPO:-https://github.com/the78mole/PredictionDB.git}
  branch=${PDB_UPDATE_BRANCH:-main}
  image=${PDB_IMAGE:-ghcr.io/the78mole/predictiondb}
  cd "$root"

  exec 9>"$root/.git/pdb-update.lock"
  if ! flock -n 9; then
    echo "Update läuft bereits, übersprungen"
    return 0
  fi
  update "$force"
}

main "$@"
exit
