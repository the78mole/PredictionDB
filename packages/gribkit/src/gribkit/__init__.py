"""GRIB2-Werkzeuge für Partial Downloads."""

from gribkit.bbox import BBox
from gribkit.grib import GribError, crop, crop_message, describe, iter_messages
from gribkit.headers import HeaderError, find_boundary, parse_head
from gribkit.idx import (
    ByteRange,
    IdxEntry,
    IdxParseError,
    MessageSpec,
    merge_ranges,
    missing_specs,
    parse_idx,
    select,
)

__all__ = [
    "BBox",
    "ByteRange",
    "GribError",
    "HeaderError",
    "IdxEntry",
    "IdxParseError",
    "MessageSpec",
    "crop",
    "crop_message",
    "describe",
    "find_boundary",
    "iter_messages",
    "merge_ranges",
    "missing_specs",
    "parse_head",
    "parse_idx",
    "select",
]
