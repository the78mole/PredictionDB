#!/usr/bin/env bash
# Selbst-Update der VM: holt main von GitHub und rollt einen neuen Stand aus
# (Image bauen, garage-init, migrate, compose up). Läuft im Compose-Dienst
# "updater" (Profil "updater") in einer Schleife wie die anderen Dienste mit
# --watch, kann aber auch von Hand auf der VM laufen.
#
#   scripts/vm-update.sh [--force] [--watch SEK]
#     --force       auch ohne neuen Commit ausrollen
#     --watch SEK   wiederholen im Abstand von SEK Sekunden
#
# Der zuletzt erfolgreich ausgerollte Commit steht in .git/pdb-deployed. Schlägt
# ein Schritt fehl, bleibt er unverändert und der nächste Lauf versucht es erneut;
# die laufenden Container bleiben dann auf dem alten Stand.
#
# Umgebung: PDB_UPDATE_REPO (Vorgabe: öffentliches GitHub-Repo per HTTPS),
#           PDB_UPDATE_BRANCH (Vorgabe: main).
set -euo pipefail

# Alles in Funktionen: reset --hard kann dieses Skript während des Laufs
# ersetzen, Bash hat die Funktionen dann aber schon vollständig gelesen.
update() {
  # Jeder Schritt mit "|| return 1": im Watch-Betrieb läuft update hinter "||",
  # dort greift set -e nicht
  local force=$1 neu alt dienste
  git fetch --quiet "$repo" "+refs/heads/$branch:refs/remotes/origin/$branch" || return 1
  neu=$(git rev-parse "origin/$branch") || return 1
  alt=$(cat .git/pdb-deployed 2>/dev/null || true)
  if [[ $neu == "$alt" && $force != 1 ]]; then
    return 0
  fi

  echo "Rolle ${neu:0:7} aus (bisher ${alt:0:7})"
  git log --oneline "${alt:-$neu~1}..$neu" 2>/dev/null | head -20 || true
  # Untracked Dateien (.env, compose.vm.yaml) bleiben bei reset --hard erhalten
  git reset --hard --quiet "$neu" || return 1
  docker build --quiet -f docker/Dockerfile -t ghcr.io/the78mole/predictiondb:local . \
    || return 1
  docker compose run --rm garage-init || return 1
  docker compose run --rm migrate || return 1
  # Den Updater selbst zuletzt: ein geänderter Updater-Dienst beendet diesen Lauf
  dienste=$(docker compose config --services | grep -vx updater) || return 1
  # shellcheck disable=SC2086
  docker compose up -d $dienste || return 1
  docker image prune -f >/dev/null || true
  echo "$neu" > .git/pdb-deployed
  echo "Ausgerollt: $(git log -1 --format='%h %s')"
  if docker compose config --services | grep -qx updater; then
    docker compose up -d --build --no-deps updater || return 1
  fi
}

main() {
  local force=0 watch=0
  while [[ $# -gt 0 ]]; do
    case $1 in
      --force) force=1 ;;
      --watch) watch=$2; shift ;;
      *) echo "unbekannte Option $1" >&2; exit 2 ;;
    esac
    shift
  done

  root=$(cd "$(dirname "$0")/.." && pwd)
  repo=${PDB_UPDATE_REPO:-https://github.com/the78mole/PredictionDB.git}
  branch=${PDB_UPDATE_BRANCH:-main}
  cd "$root"
  # Im Container gehört das Repo einem anderen Benutzer
  git config --global --add safe.directory "$root" 2>/dev/null || true

  exec 9>"$root/.git/pdb-update.lock"
  while true; do
    if ! flock -n 9; then
      echo "Update läuft bereits, übersprungen"
    elif [[ $watch -le 0 ]]; then
      update "$force"
      return
    else
      update "$force" || echo "Update fehlgeschlagen, nächster Versuch in ${watch}s" >&2
      flock -u 9
    fi
    [[ $watch -le 0 ]] && return 0
    force=0
    sleep "$watch"
  done
}

main "$@"
exit
