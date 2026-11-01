#!/usr/bin/env bash
# Rohdaten für die Auswertung laden: GFS-0p5-Silber (GHCR, öffentlich) und Energy-Charts.
# Aufruf: docs/analysen/standortgewichte-lernen/daten_laden.sh ARBEITSORDNER
set -euo pipefail
W=${1:?Arbeitsordner}; H=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$W/data" "$W/ec"
R=the78mole/predictiondb-data/gfs-0p5-silver
curl -s "https://ghcr.io/token?scope=repository:$R:pull" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])' > "$W/tok"
for c in de pl at es; do
  for y in 2024 2025; do
    curl -s "https://api.energy-charts.info/public_power?country=$c&start=$y-01-01T00:00Z&end=$y-12-31T23:59Z" > "$W/ec/pp_${c}_$y.json"
  done
  curl -s "https://api.energy-charts.info/installed_power?country=$c&time_step=yearly&installation_decommission=false" > "$W/ec/ipy_$c.json"
done
uv run --with numcodecs python -I "$H/fetch_gfs.py" "$W/data" "$W/tok"
