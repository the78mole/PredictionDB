from datetime import date

import numpy as np
import pytest

from mastr_ingest.capacity import daily_capacity
from mastr_ingest.cluster import nearest, weighted_kmeans
from mastr_ingest.pipeline import quarter_bounds, version_name


def _blob(lat: float, lon: float, n: int, rng: np.random.Generator) -> np.ndarray:
    return np.column_stack((lat + rng.normal(0, 0.2, n), lon + rng.normal(0, 0.2, n)))


def test_kmeans_separates_regions_and_orders_by_capacity() -> None:
    rng = np.random.default_rng(1)
    north, south = _blob(54.0, 9.0, 40, rng), _blob(48.0, 11.5, 40, rng)
    pts = np.vstack((north, south))
    w = np.r_[np.full(40, 1.0), np.full(40, 5.0)]  # Süden hat mehr Leistung
    cl = weighted_kmeans(pts[:, 0], pts[:, 1], w, 2)
    assert cl.k == 2
    assert cl.labels[:40].tolist() == [1] * 40 and cl.labels[40:].tolist() == [0] * 40
    assert cl.centroid_lat[0] == pytest.approx(48.0, abs=0.15)
    assert cl.capacity.tolist() == pytest.approx([200.0, 40.0])


def test_kmeans_weighted_centroid() -> None:
    lat, lon = np.array([50.0, 52.0]), np.array([10.0, 10.0])
    cl = weighted_kmeans(lat, lon, np.array([3.0, 1.0]), 1)
    assert cl.centroid_lat[0] == pytest.approx(50.5)


def test_kmeans_deterministic_and_k_capped() -> None:
    rng = np.random.default_rng(2)
    pts = _blob(51, 10, 30, rng)
    w = rng.uniform(1, 10, 30)
    a = weighted_kmeans(pts[:, 0], pts[:, 1], w, 5, seed=3)
    b = weighted_kmeans(pts[:, 0], pts[:, 1], w, 5, seed=3)
    assert np.array_equal(a.labels, b.labels)
    w0 = np.r_[np.ones(3), np.zeros(27)]
    assert weighted_kmeans(pts[:, 0], pts[:, 1], w0, 10).k == 3


def test_nearest() -> None:
    idx = nearest(
        np.array([54.1, 47.9]), np.array([9.0, 11.0]), np.array([48.0, 54.0]), np.array([11.5, 9.0])
    )
    assert idx.tolist() == [1, 0]


def test_daily_capacity_events() -> None:
    start, end = date(2020, 1, 1), date(2020, 1, 10)
    events = [
        (date(2019, 6, 1), 100.0),  # vor Beginn → Anfangsbestand
        (date(2020, 1, 3), 50.0),
        (date(2020, 1, 5), -100.0),  # Stilllegung wirkt ab dem Tag
        (date(2020, 1, 8), -50.0),
        (date(2020, 1, 9), 50.0),  # Wiederaufnahme
        (date(2021, 1, 1), 999.0),  # nach Stichtag
    ]
    assert daily_capacity(events, start, end).tolist() == [
        100,
        100,
        150,
        150,
        50,
        50,
        50,
        0,
        50,
        50,
    ]
    with pytest.raises(ValueError):
        daily_capacity([], end, start)


def test_quarters_and_versions() -> None:
    assert quarter_bounds(date(2026, 10, 20)) == (date(2026, 10, 1), date(2026, 12, 31))
    assert quarter_bounds(date(2026, 2, 28)) == (date(2026, 1, 1), date(2026, 3, 31))
    assert version_name(date(2026, 10, 1), set()) == "v2026Q4"
    assert version_name(date(2026, 10, 1), {"v2026Q4", "v2026Q4b"}) == "v2026Q4c"
