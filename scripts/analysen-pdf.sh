#!/usr/bin/env bash
# Rendert jede Analyse unter docs/analysen/<name>/README.md samt Grafiken zu
# <ziel>/analyse-<name>.pdf. Markdown -> HTML mit Pandoc (Bilder eingebettet),
# HTML -> PDF mit Chromium im Headless-Modus.
#
#   scripts/analysen-pdf.sh [ziel]          Standardziel: dist/analysen
#
# Umgebung: CHROME (Pfad zu Chrome/Chromium, sonst Suche im PATH),
#           PDF_VERSION (Text in der Fußzeile, sonst git describe),
#           PDF_REPO_URL und PDF_REF (Ziel relativer Links, sonst
#           https://github.com/the78mole/predictiondb und der aktuelle Commit).
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
ziel=$(mkdir -p "${1:-$root/dist/analysen}" && cd "${1:-$root/dist/analysen}" && pwd)
css="$root/scripts/analysen-pdf.css"

chrome=${CHROME:-}
if [[ -z $chrome ]]; then
  for c in google-chrome chromium chromium-browser chrome; do
    if command -v "$c" >/dev/null; then chrome=$(command -v "$c"); break; fi
  done
fi
[[ -n $chrome ]] || { echo "Kein Chrome/Chromium gefunden (CHROME setzen)" >&2; exit 1; }

version=${PDF_VERSION:-$(git -C "$root" describe --tags --always --dirty 2>/dev/null || echo unbekannt)}
repo_url=${PDF_REPO_URL:-https://github.com/the78mole/predictiondb}
ref=${PDF_REF:-$(git -C "$root" rev-parse HEAD 2>/dev/null || echo main)}
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

anzahl=0
for readme in "$root"/docs/analysen/*/README.md; do
  dir=$(dirname "$readme")
  name=$(basename "$dir")
  titel=$(grep -m1 '^# ' "$readme" | sed 's/^# //')
  stand=$(git -C "$root" log -1 --format=%cs -- "$dir" 2>/dev/null || true)

  # Fußzeile links: Analyse, Stand der letzten Änderung, Version des Repos.
  fuss="PredictionDB · $name${stand:+ · Stand $stand} · $version"
  fuss=${fuss//\\/\\\\}
  fuss=${fuss//\"/\\\"}
  cat >"$tmp/kopf.html" <<HTML
<style>@page { @bottom-left { content: "$fuss"; font: 8pt "DejaVu Sans", sans-serif; color: #666; } }</style>
HTML

  pandoc "$readme" \
    --from gfm --to html5 --standalone --embed-resources \
    --resource-path "$dir" \
    --metadata pagetitle="$titel" --metadata lang=de \
    --metadata link-basis="$repo_url/blob/$ref/docs/analysen/$name/" \
    --lua-filter "$root/scripts/analysen-pdf-links.lua" \
    --css "$css" --include-in-header "$tmp/kopf.html" \
    --output "$tmp/$name.html"

  "$chrome" --headless --no-sandbox --disable-gpu --no-pdf-header-footer \
    --run-all-compositor-stages-before-draw --virtual-time-budget=10000 \
    --print-to-pdf="$ziel/analyse-$name.pdf" "file://$tmp/$name.html" 2>/dev/null

  [[ -s "$ziel/analyse-$name.pdf" ]] || { echo "PDF für $name fehlt" >&2; exit 1; }
  echo "$ziel/analyse-$name.pdf"
  anzahl=$((anzahl + 1))
done

[[ $anzahl -gt 0 ]] || { echo "Keine Analysen unter docs/analysen/*/README.md" >&2; exit 1; }
