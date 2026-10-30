import obstore
from obstore.store import MemoryStore

from wx_process.cli import build_parser
from wx_process.storage import OTHER, Usage, group_of, measure_bucket


def test_groups_by_model_and_resolution() -> None:
    """``gfs/0p5`` darf nicht unter ``gfs/0p25`` fallen; fremde Präfixe zählen als sonstige.

    @relation(SWR-WXP-050, scope=function)
    """
    assert group_of("gfs/0p25/2026/10/30/00/f003.grib2") == "gfs_0p25"
    assert group_of("gfs/0p5/2026/10/30/00.zarr/zarr.json") == "gfs_0p5"
    assert group_of("gefs/0p5/2026/10/30/00.tar.gz") == "gefs_0p5"
    assert group_of("gfs/1p0/2026/10/30/00/f003.grib2") == OTHER
    assert group_of("gfs/0p25") == OTHER


def test_measure_bucket_sums_size_and_count() -> None:
    """@relation(SWR-WXP-050, scope=function)"""
    store = MemoryStore()
    for path, size in (
        ("gfs/0p25/2026/10/30/00/f000.grib2", 100),
        ("gfs/0p25/2026/10/30/00/f003.grib2", 50),
        ("gfs/0p5/2026/10/30/00/f000.grib2", 7),
        ("gefs/0p5/2026/10/30/00/c00.f000.grib2", 3),
        ("tmp/upload", 1),
    ):
        obstore.put(store, path, b"x" * size)
    assert measure_bucket("bronze", store) == [
        Usage("bronze", "gefs_0p5", 3, 1),
        Usage("bronze", "gfs_0p25", 150, 2),
        Usage("bronze", "gfs_0p5", 7, 1),
        Usage("bronze", OTHER, 1, 1),
    ]


def test_storage_command() -> None:
    """@relation(SWR-WXP-050, scope=function)"""
    args = build_parser().parse_args(["storage", "--watch", "21600"])
    assert args.model == "storage" and args.watch == 21600
