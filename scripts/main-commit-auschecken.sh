#!/usr/bin/env bash
# Checkt einen Commit aus, aber nur, wenn er im Verlauf des ausgecheckten Stands
# liegt (bei workflow_run ist das der Default-Branch main).
#
# Workflows mit workflow_run-Auslöser laufen mit Schreibrechten. Würden sie
# github.event.workflow_run.head_sha direkt auschecken, könnte ein Fork-PR
# eigenen Code in diesen Kontext bringen ("untrusted checkout", OpenSSF
# Scorecard Dangerous-Workflow). Deshalb checkt actions/checkout immer den
# Default-Branch aus, und dieses Skript wechselt danach nur auf Commits, die
# bereits auf main liegen.
#
#   scripts/main-commit-auschecken.sh <sha>
#
# Braucht den vollen Verlauf (actions/checkout mit fetch-depth: 0).
set -euo pipefail

sha=${1:?Commit fehlt}
if [[ ! $sha =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error::Kein vollständiger Commit-Hash: $sha" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$sha" HEAD; then
  echo "::error::Commit $sha liegt nicht auf $(git rev-parse --abbrev-ref HEAD), Abbruch" >&2
  exit 1
fi
git checkout --quiet --detach "$sha"
echo "Ausgecheckt: $(git log -1 --format='%h %s')"
