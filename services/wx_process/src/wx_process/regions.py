"""Feste Regionen statt MaStR-Cluster (GFS Europa: dieselben Gebotszonen wie GEFS).

Die Datei ist dieselbe wie bei GEFS (``config/gefs/regions_v2.json.gz``, gebaut von
``wx-exchange gefs regions``): je Region Technologie und Zellen ``(lat, lon, Gewicht)`` auf dem
0,5°-Gitter. Die Regionen landen unter denselben Codes ``gefs:<code>`` in ``meta.region`` wie
die GEFS-Kennzahlen, damit sich GFS und GEFS je Zone direkt vergleichen lassen.

@relation(SWA-WXP-REGIONS, scope=file)
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from wx_process.features import F64, ClusterWeights

#: Präfix der Codes in ``meta.region`` (wie bei GEFS)
REGION_PREFIX = "gefs:"


@dataclass(frozen=True)
class Region:
    code: str
    technology: str
    lat: F64
    lon: F64
    share: F64
    hub_height_m: float | None = None


@dataclass(frozen=True)
class RegionSet:
    #: Version der Regionen, steht als ``clustering_version`` in ``meta.nwp_feature_run``
    version: str
    regions: list[Region]

    @classmethod
    def load(cls, path: Path) -> RegionSet:
        """@relation(SWR-WXP-035, scope=function)"""
        raw = path.read_bytes()
        data = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        regions = []
        for r in data["regions"]:
            cells = np.asarray(r["cells"], dtype=np.float64).reshape(-1, 3)
            regions.append(
                Region(
                    r["code"],
                    r["technology"],
                    cells[:, 0],
                    cells[:, 1],
                    cells[:, 2] / cells[:, 2].sum(),
                    r.get("hub_height_m"),
                )
            )
        return cls(f"regions_{data['version']}", regions)

    @property
    def codes(self) -> list[str]:
        return [REGION_PREFIX + r.code for r in self.regions]

    def clusters(self, region_ids: Mapping[str, int]) -> list[ClusterWeights]:
        """Je Region ein „Cluster“; ``region_ids`` bildet ``gefs:<code>`` auf ``region_id`` ab.

        @relation(SWR-WXP-035, scope=function)
        """
        return [
            ClusterWeights(
                r.technology,
                no,
                region_ids[REGION_PREFIX + r.code],
                r.lat,
                r.lon,
                r.share,
                r.hub_height_m,
            )
            for no, r in enumerate(self.regions)
        ]
