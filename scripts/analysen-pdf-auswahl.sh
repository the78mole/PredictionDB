#!/usr/bin/env bash
# Entscheidet je Analyse unter docs/analysen/<name>/, ob ihr PDF neu gerendert
# werden muss oder aus dem Release <basis> übernommen werden kann. Neu gerendert
# wird, wenn sich der Ordner seit <basis> geändert hat, wenn sich der Renderer
# (scripts/analysen-pdf*, Workflow) geändert hat oder wenn es keine Basis gibt.
#
#   scripts/analysen-pdf-auswahl.sh [basis] [ziel]     Standardziel: HEAD
#
# Ausgabe (auch nach $GITHUB_OUTPUT, wenn gesetzt), Namen durch Leerzeichen getrennt:
#   rendern=<namen>  uebernehmen=<namen>
set -euo pipefail

basis=${1:-}
ziel=${2:-HEAD}
root=$(cd "$(dirname "$0")/.." && pwd)
renderer=('scripts/analysen-pdf*' .github/workflows/analysen-pdf.yml)

alle=()
for readme in "$root"/docs/analysen/*/README.md; do
  alle+=("$(basename "$(dirname "$readme")")")
done

rendern=()
uebernehmen=()
if [[ -z $basis ]] || ! git -C "$root" rev-parse -q --verify "$basis^{commit}" >/dev/null \
  || ! git -C "$root" diff --quiet "$basis" "$ziel" -- "${renderer[@]}"; then
  rendern=("${alle[@]}")
else
  for name in "${alle[@]}"; do
    if git -C "$root" diff --quiet "$basis" "$ziel" -- "docs/analysen/$name"; then
      uebernehmen+=("$name")
    else
      rendern+=("$name")
    fi
  done
fi

{
  echo "rendern=${rendern[*]}"
  echo "uebernehmen=${uebernehmen[*]}"
} | tee -a "${GITHUB_OUTPUT:-/dev/null}"
