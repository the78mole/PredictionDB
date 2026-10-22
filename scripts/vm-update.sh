#!/usr/bin/env bash
# Selbst-Update der VM: holt main von GitHub und rollt einen neuen Stand aus
# (Image bauen, garage-init, migrate, compose up). Läuft per systemd-Timer
# (deploy/systemd/predictiondb-update.timer), kann aber auch von Hand laufen.
#
#   scripts/vm-update.sh [--force]      --force: auch ohne neuen Commit ausrollen
#
# Der zuletzt erfolgreich ausgerollte Commit steht in .git/pdb-deployed. Schlägt
# ein Schritt fehl, bleibt er unverändert und der nächste Lauf versucht es erneut;
# die laufenden Container bleiben dann auf dem alten Stand.
#
# Umgebung: PDB_UPDATE_REPO (Vorgabe: öffentliches GitHub-Repo per HTTPS),
#           PDB_UPDATE_BRANCH (Vorgabe: main).
set -euo pipefail

# Alles in einer Funktion: reset --hard kann dieses Skript während des Laufs
# ersetzen, Bash hat die Funktion dann aber schon vollständig gelesen.
main() {
  root=$(cd "$(dirname "$0")/.." && pwd)
  repo=${PDB_UPDATE_REPO:-https://github.com/the78mole/PredictionDB.git}
  branch=${PDB_UPDATE_BRANCH:-main}
  cd "$root"

  exec 9>"$root/.git/pdb-update.lock"
  if ! flock -n 9; then
    echo "Update läuft bereits, Abbruch"
    exit 0
  fi

  git fetch --quiet "$repo" "+refs/heads/$branch:refs/remotes/origin/$branch"
  neu=$(git rev-parse "origin/$branch")
  alt=$(cat .git/pdb-deployed 2>/dev/null || true)

  if [[ $neu == "$alt" && ${1:-} != --force ]]; then
    exit 0
  fi

  echo "Rolle ${neu:0:7} aus (bisher ${alt:0:7})"
  git log --oneline "${alt:-$neu~1}..$neu" 2>/dev/null | head -20 || true
  # Untracked Dateien (.env, compose.vm.yaml) bleiben bei reset --hard erhalten
  git reset --hard --quiet "$neu"
  docker build --quiet -f docker/Dockerfile -t ghcr.io/the78mole/predictiondb:local .
  docker compose run --rm garage-init
  docker compose run --rm migrate
  docker compose up -d
  docker image prune -f >/dev/null
  echo "$neu" > .git/pdb-deployed
  echo "Ausgerollt: $(git log -1 --format='%h %s')"
}

main "$@"
exit
