"""Day-Ahead-Preis DE-LU, Erzeugung, Last, Außenhandel, Wind-/PV-Prognosen 2025 (15 min).

Quelle: Energy-Charts (Fraunhofer ISE).

Energy-Charts gibt für Preis, öffentliche Nettoerzeugung und kommerziellen Außenhandel die
Lizenz CC BY 4.0 an (Preis: Bundesnetzagentur | SMARD.de), die Prognosen tragen keine eigene
Angabe, es gilt die API-Regel CC BY 4.0. Aufruf::

    uv run --no-project --with requests python abruf.py ./daten-2025
"""

import sys
from pathlib import Path

import requests  # type: ignore[import-untyped]

B = "https://api.energy-charts.info"
ZEIT = "start=2024-12-01T00:00%2B01:00&end=2026-01-01T00:00%2B01:00"
AUFRUFE = {
    "price": f"{B}/price?bzn=DE-LU&{ZEIT}",
    "public_power": f"{B}/public_power?country=de&{ZEIT}",
} | {
    f"fc_{t}": f"{B}/public_power_forecast?country=de&production_type={t}"
    f"&forecast_type=day-ahead&{ZEIT}"
    for t in ["solar", "wind_onshore", "wind_offshore"]
}

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
for name, url in AUFRUFE.items():
    r = requests.get(url, timeout=300)
    r.raise_for_status()
    (out / f"{name}.json").write_bytes(r.content)
    print(name, len(r.content))
