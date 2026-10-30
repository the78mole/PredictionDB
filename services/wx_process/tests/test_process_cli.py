from wx_process.cli import build_parser


def test_backfill_runs_keep_priority_order() -> None:
    """``--runs 0,12,6,18`` heißt: erst alle 00Z, dann 12Z, 06Z, 18Z (nicht sortiert).

    @relation(SWR-WXP-040, scope=function)
    """
    args = build_parser().parse_args(
        ["gfs", "backfill", "2021-03-22T12", "--runs", "0,12,6,18,12", "--decompress"]
    )
    assert args.runs == [0, 12, 6, 18] and args.decompress
