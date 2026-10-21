import argparse

import pytest

from wx_ingest.cli import build_parser, parse_hours


def test_backfill_args() -> None:
    args = build_parser().parse_args(
        ["gfs", "backfill", "2021-03-22T12", "--runs", "12,0", "--newest-first"]
    )
    assert f"{args.start:%Y%m%d%H}" == "2021032212"
    assert args.end is None  # offenes Ende = jetzt
    assert args.runs == [0, 12] and args.newest_first and args.parallel_runs == 1
    assert args.archive_concurrency is None


def test_backfill_concurrency() -> None:
    args = build_parser().parse_args(
        ["gfs", "backfill", "2021-03-22T12", "--parallel-runs", "12", "--archive-concurrency", "48"]
    )
    assert (args.parallel_runs, args.archive_concurrency) == (12, 48)


@pytest.mark.parametrize("text", ["3", "0,x", ""])
def test_invalid_hours(text: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_hours(text)
