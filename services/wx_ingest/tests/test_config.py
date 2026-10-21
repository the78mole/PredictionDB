from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from wx_ingest.config import GfsConfig
from wx_ingest.testing import config_dict, make_config

REPO = Path(__file__).resolve().parents[3]


def test_repo_config_matches_architecture() -> None:
    cfg = GfsConfig.from_yaml(REPO / "config/sources/gfs_0p25.yaml")
    steps = cfg.step_list()
    assert (
        len(steps) == 209
        and steps[0] == 0
        and steps[120] == 120
        and steps[121] == 123
        and steps[-1] == 384
    )
    assert [e.name for e in cfg.endpoints] == ["nomads", "aws", "gcs"]
    assert [e.name for e in cfg.endpoints_for(archive_only=True)] == ["aws", "gcs"]
    b = cfg.bbox.bbox()
    assert (b.north, b.south, b.west, b.east) == (58, 44, 0, 20)
    assert len(cfg.specs()) == 22


def test_runs_between_and_recent() -> None:
    cfg = make_config(runs=[0, 12])
    runs = cfg.runs_between(
        datetime(2026, 10, 19, 5, tzinfo=UTC), datetime(2026, 10, 20, 12, tzinfo=UTC)
    )
    assert [f"{r:%d%H}" for r in runs] == ["1912", "2000", "2012"]
    recent = cfg.recent_runs(datetime(2026, 10, 20, 13, 30, tzinfo=UTC))
    assert [f"{r:%d%H}" for r in recent] == ["1912", "2000", "2012"]
    # Teilmenge der konfigurierten Läufe; nicht konfigurierte Stunden fallen weg
    only = cfg.runs_between(runs[0], runs[-1], hours=[12, 18])
    assert [f"{r:%d%H}" for r in only] == ["1912", "2012"]


@pytest.mark.parametrize(
    "over",
    [
        {"runs": [3]},
        {"endpoints": []},
        {"messages": []},
        {"steps": [[10, 0, 1]]},
        {"bbox": {"north": 40, "south": 50, "west": 0, "east": 20}},
    ],
)
def test_invalid_config(over: dict[str, Any]) -> None:
    with pytest.raises((ValidationError, ValueError)):
        GfsConfig.model_validate(config_dict(**over))
