"""Prüfung von Artefakten aus GHCR: Tar-Inhalte, Digests, Manifest-Felder.

Artefakte stammen von anderen Instanzen und sind nicht vertrauenswürdig. Alles, was hier nicht
passt, endet in ``ArtifactError`` statt in beliebigen Ausnahmen (``KeyError``,
``tarfile.ReadError``, ``zlib.error`` …), in Pfaden außerhalb des Ziel-Präfix oder in einem
Tar, der beim Entpacken den Speicher sprengt.

@relation(SWA-WXX-SAFE, scope=file)
"""

from __future__ import annotations

import gzip
import io
import json
import re
import tarfile
import zlib
from typing import Any

#: Obergrenzen je Tar; ein Silber-Lauf hat ≈ 100 Dateien und wenige MB, ein idx-Monat ≈ 30 000
MAX_FILES = 200_000
MAX_BYTES = 4 * 1024**3

_DIGEST = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


class ArtifactError(ValueError):
    """Inhalt eines Artefakts aus GHCR ist ungültig.

    @relation(SWR-WXX-060, scope=class)
    """


def safe_name(name: str) -> str:
    """Relativer Pfad ohne ``..``, ohne leere Teile und ohne Steuerzeichen.

    @relation(SWR-WXX-060, scope=function)
    """
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(p in ("", ".", "..") for p in parts)
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name)
    ):
        raise ArtifactError(f"unzulässiger Pfad im Tar: {name!r}")
    return name


def read_tar(
    data: bytes,
    *,
    compressed: bool = False,
    max_files: int = MAX_FILES,
    max_bytes: int = MAX_BYTES,
) -> dict[str, bytes]:
    """Reguläre Dateien eines Tars nach Namen; andere Einträge (Ordner, Links) werden ignoriert.

    ``compressed`` erwartet gzip. Doppelte Namen, unzulässige Pfade und zu viele oder zu große
    Dateien sind ein ``ArtifactError``.

    @relation(SWR-WXX-060, scope=function)
    """
    try:
        if compressed:
            data = _gunzip(data, max_bytes)
        out: dict[str, bytes] = {}
        total = 0
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            for i, m in enumerate(tar):
                if i >= max_files:
                    raise ArtifactError(f"mehr als {max_files} Einträge im Tar")
                if not m.isfile():
                    continue
                name = safe_name(m.name)
                if name in out:
                    raise ArtifactError(f"doppelter Pfad im Tar: {name!r}")
                total += m.size
                if total > max_bytes:
                    raise ArtifactError(f"Tar größer als {max_bytes} Bytes")
                f = tar.extractfile(m)
                body = f.read() if f is not None else b""
                if len(body) != m.size:
                    raise ArtifactError(f"Tar abgeschnitten bei {name!r}")
                out[name] = body
        return out
    except ArtifactError:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, UnicodeError) as exc:
        raise ArtifactError(f"Tar nicht lesbar: {type(exc).__name__}: {exc}") from exc


def _gunzip(data: bytes, max_bytes: int) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        out = gz.read(max_bytes + 1)
    if len(out) > max_bytes:
        raise ArtifactError(f"entpackt größer als {max_bytes} Bytes")
    return out


def check_digest(value: Any) -> str:
    """``sha256:<64 hex>``; alles andere landet sonst ungeprüft in Registry-URLs.

    @relation(SWR-WXX-060, scope=function)
    """
    if not isinstance(value, str) or not _DIGEST.match(value):
        raise ArtifactError(f"ungültiger Digest: {value!r}")
    return value


def json_object(data: bytes | str) -> dict[str, Any]:
    """JSON, das ein Objekt sein muss.

    @relation(SWR-WXX-060, scope=function)
    """
    try:
        out = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise ArtifactError(f"kein gültiges JSON: {exc}") from exc
    if not isinstance(out, dict):
        raise ArtifactError(f"JSON-Objekt erwartet, nicht {type(out).__name__}")
    return out


def annotations_of(d: Any) -> dict[str, str]:
    """Annotationen eines Manifests oder Deskriptors (fehlend = leer).

    @relation(SWR-WXX-060, scope=function)
    """
    if not isinstance(d, dict):
        raise ArtifactError(f"Objekt erwartet, nicht {type(d).__name__}")
    a = d.get("annotations", {})
    if not isinstance(a, dict) or not all(isinstance(v, str) for v in a.values()):
        raise ArtifactError("Annotationen müssen Texte sein")
    return a


def layers(m: Any) -> list[dict[str, Any]]:
    """Layer-Deskriptoren eines Manifests (fehlend = leer).

    @relation(SWR-WXX-060, scope=function)
    """
    if not isinstance(m, dict):
        raise ArtifactError(f"Manifest muss ein Objekt sein, nicht {type(m).__name__}")
    out = m.get("layers", [])
    if not isinstance(out, list) or not all(isinstance(d, dict) for d in out):
        raise ArtifactError("layers muss eine Liste von Objekten sein")
    return out


GUARDED = (KeyError, ValueError, TypeError, AttributeError, OverflowError)
