"""Leistungsgewichtetes k-Means auf Gitterzellen.

Geclustert wird nicht über Einzelanlagen, sondern über die je Gitterzelle summierte Leistung
(Zellen des Wettermodellgitters, Standard GFS 0,25°). Das hält die Rechnung klein (einige
tausend Punkte statt Millionen Anlagen), und jede Zelle gehört genau einem Cluster, sodass die
Gewichtsmatrix je Cluster disjunkt ist. Abstände werden in einer flächentreuen Näherung
(Längengrade mit cos(Breite) gestaucht) gerechnet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

KM_PER_DEG = 111.32
REF_LAT = 51.0


def _xy(lat: NDArray[np.float64], lon: NDArray[np.float64]) -> NDArray[np.float64]:
    return np.column_stack((lon * math.cos(math.radians(REF_LAT)) * KM_PER_DEG, lat * KM_PER_DEG))


def _latlon(xy: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    return xy[:, 1] / KM_PER_DEG, xy[:, 0] / (math.cos(math.radians(REF_LAT)) * KM_PER_DEG)


@dataclass
class Clustering:
    centroid_lat: NDArray[np.float64]
    centroid_lon: NDArray[np.float64]
    #: Cluster je Eingangspunkt, nach Leistung absteigend nummeriert (0 = größtes)
    labels: NDArray[np.int64]
    capacity: NDArray[np.float64]

    @property
    def k(self) -> int:
        return len(self.centroid_lat)


def _sq_dist(points: NDArray[np.float64], centers: NDArray[np.float64]) -> NDArray[np.float64]:
    d: NDArray[np.float64] = ((points[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
    return d


def weighted_kmeans(
    lat: NDArray[np.float64],
    lon: NDArray[np.float64],
    weight: NDArray[np.float64],
    k: int,
    *,
    seed: int = 0,
    n_init: int = 8,
    max_iter: int = 300,
) -> Clustering:
    """k-Means mit Gewichten (k-means++-Start, Lloyd), deterministisch bei gleichem ``seed``.

    Aus ``n_init`` Starts gewinnt die kleinste gewichtete Quadratsumme. Ist ``k`` größer als
    die Zahl der Punkte mit Gewicht, wird es auf diese begrenzt.
    """
    if not (len(lat) == len(lon) == len(weight)) or len(lat) == 0:
        raise ValueError("lat, lon und weight brauchen dieselbe Länge > 0")
    pts = _xy(np.asarray(lat, float), np.asarray(lon, float))
    w = np.clip(np.asarray(weight, float), 0, None)
    if w.sum() <= 0:
        w = np.ones_like(w)
    k = max(1, min(k, int((w > 0).sum())))
    rng = np.random.default_rng(seed)

    best: tuple[float, NDArray[np.float64], NDArray[np.int64]] | None = None
    for _ in range(n_init):
        centers = _init_pp(pts, w, k, rng)
        labels = np.zeros(len(pts), dtype=np.int64)
        for it in range(max_iter):
            labels_new = _sq_dist(pts, centers).argmin(axis=1)
            if it and np.array_equal(labels_new, labels):
                break
            labels = labels_new
            for c in range(k):
                m = labels == c
                if w[m].sum() > 0:
                    centers[c] = np.average(pts[m], axis=0, weights=w[m])
                else:  # leerer Cluster: auf den Punkt mit dem größten Beitrag zum Fehler setzen
                    err = w * _sq_dist(pts, centers).min(axis=1)
                    centers[c] = pts[int(err.argmax())]
        inertia = float((w * _sq_dist(pts, centers)[np.arange(len(pts)), labels]).sum())
        if best is None or inertia < best[0]:
            best = (inertia, centers.copy(), labels.copy())
    assert best is not None
    _, centers, labels = best

    cap = np.bincount(labels, weights=w, minlength=k)
    order = np.argsort(-cap, kind="stable")
    rank = np.empty(k, dtype=np.int64)
    rank[order] = np.arange(k)
    clat, clon = _latlon(centers[order])
    return Clustering(clat, clon, rank[labels], cap[order])


def _init_pp(
    pts: NDArray[np.float64], w: NDArray[np.float64], k: int, rng: np.random.Generator
) -> NDArray[np.float64]:
    centers = np.empty((k, 2))
    centers[0] = pts[rng.choice(len(pts), p=w / w.sum())]
    d = _sq_dist(pts, centers[:1])[:, 0]
    for i in range(1, k):
        p = w * d
        total = p.sum()
        idx = rng.choice(len(pts), p=p / total) if total > 0 else int(rng.integers(len(pts)))
        centers[i] = pts[idx]
        d = np.minimum(d, _sq_dist(pts, centers[i : i + 1])[:, 0])
    return centers


def nearest(
    lat: NDArray[np.float64],
    lon: NDArray[np.float64],
    centroid_lat: NDArray[np.float64],
    centroid_lon: NDArray[np.float64],
) -> NDArray[np.int64]:
    """Index des nächstgelegenen Zentrums je Punkt (für neue Zellen bestehender Clusterungen)."""
    centers = _xy(np.asarray(centroid_lat, float), np.asarray(centroid_lon, float))
    pts = _xy(np.asarray(lat, float), np.asarray(lon, float))
    out: NDArray[np.int64] = _sq_dist(pts, centers).argmin(axis=1)
    return out
