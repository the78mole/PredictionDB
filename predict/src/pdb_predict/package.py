"""Modellpaket laden (Aufbau siehe ``ml_train.bundle``), lokal oder aus einer OCI-Registry.

Ein Paket ist ein Ordner mit ``package.json``. Eine OCI-Referenz (``ghcr.io/…:champion``) wird
als ORAS-Artefakt geholt: jede Schicht ist eine Datei, ihr Name steht in der Annotation
``org.opencontainers.image.title``. Öffentliche Pakete in GHCR brauchen keinen Login; das
Token holt der Dienst anonym über den ``WWW-Authenticate``-Hinweis der Registry.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from ml_train.bundle import FORMAT
from ml_train.config import ModelConfig, load_config
from ml_train.models import TwoStageModel
from predictiondb_core.log import get_logger

log = get_logger(__name__)

TITLE = "org.opencontainers.image.title"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
REF = re.compile(r"^(?:oci://)?(?P<host>[^/]+\.[^/]+)/(?P<name>[^:@]+)(?::(?P<tag>[\w.-]+))?$")


@dataclass
class ModelPackage:
    path: Path
    info: dict[str, Any]
    cfg: ModelConfig
    model: TwoStageModel
    manifest: dict[str, Any]
    capacity: pd.DataFrame
    clusters: pd.DataFrame | None = None
    cluster_capacity: pd.DataFrame | None = None
    wx_process: dict[str, Any] | None = None
    source: str = ""
    digest: str | None = None

    @property
    def version(self) -> str:
        return str(self.info.get("version") or self.path.name)

    @property
    def codes(self) -> list[str]:
        return list(self.info["codes"])

    @property
    def requires_weather(self) -> bool:
        return bool(self.info.get("requires_weather"))

    def describe(self) -> dict[str, Any]:
        """Kurzinfo für ``/v1/model`` und die Prognosedatei."""
        return {
            "version": self.version,
            "config": self.info.get("config"),
            "variant": self.info.get("variant"),
            "source": self.source,
            "digest": self.digest,
            "license": self.info.get("license"),
            "attribution": self.info.get("attribution"),
            "train_issue_days": self.manifest.get("train_issue_days"),
            "capacity_until": self.info.get("capacity_until"),
            "requires_weather": self.requires_weather,
            "backtest_metrics": self.manifest.get("backtest_metrics", []),
        }


def load_package(path: Path, source: str = "", digest: str | None = None) -> ModelPackage:
    info = json.loads((path / "package.json").read_text())
    if info.get("format") != FORMAT:
        raise ValueError(f"{path}: unbekanntes Paketformat {info.get('format')!r}")
    cfg, _ = load_config(path / "config.yaml")
    model = TwoStageModel.load(cfg, path)
    manifest = json.loads((path / "manifest.json").read_text())
    capacity = pd.read_parquet(path / "capacity.parquet")
    clusters = cluster_capacity = None
    wx = None
    if info.get("requires_weather"):
        import yaml

        clusters = pd.read_parquet(path / "clusters.parquet")
        cluster_capacity = pd.read_parquet(path / "cluster_capacity.parquet")
        wx = yaml.safe_load((path / "wx_process.yaml").read_text())
    return ModelPackage(
        path,
        info,
        cfg,
        model,
        manifest,
        capacity,
        clusters,
        cluster_capacity,
        wx,
        source or str(path),
        digest,
    )


# --- OCI ---------------------------------------------------------------------------------------


def parse_ref(ref: str) -> tuple[str, str, str] | None:
    """``(host, name, tag)`` einer OCI-Referenz, ``None`` für einen lokalen Pfad."""
    if ref.startswith(("/", ".")):
        return None
    m = REF.match(ref)
    if m is None:
        return None
    return m["host"], m["name"], m["tag"] or "latest"


class OciClient:
    """Minimaler, anonymer Lesezugriff auf eine OCI-Registry (Distribution Spec v2)."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._tokens: dict[tuple[str, str], str] = {}

    def _get(self, host: str, name: str, path: str, accept: str | None = None) -> httpx.Response:
        url = f"https://{host}/v2/{name}/{path}"
        headers = {"Accept": accept} if accept else {}
        token = self._tokens.get((host, name))
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = self.client.get(url, headers=headers, follow_redirects=True)
        if r.status_code == 401 and not token:
            self._tokens[(host, name)] = self._token(r.headers.get("WWW-Authenticate", ""))
            return self._get(host, name, path, accept)
        r.raise_for_status()
        return r

    def _token(self, challenge: str) -> str:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not realm:
            raise httpx.HTTPError(f"Registry ohne Bearer-Anmeldung: {challenge!r}")
        r = self.client.get(realm, params=params)
        r.raise_for_status()
        body = r.json()
        return str(body.get("token") or body.get("access_token"))

    def resolve(self, host: str, name: str, tag: str) -> tuple[str, dict[str, Any]]:
        """Digest und Inhalt des Manifests zu einem Tag."""
        r = self._get(host, name, f"manifests/{tag}", MANIFEST_TYPES)
        digest = r.headers.get("Docker-Content-Digest") or (
            "sha256:" + hashlib.sha256(r.content).hexdigest()
        )
        return digest, dict(r.json())

    def pull(self, host: str, name: str, manifest: dict[str, Any], out: Path) -> None:
        tmp = out.with_name(out.name + ".part")
        tmp.mkdir(parents=True, exist_ok=True)
        for layer in manifest.get("layers", []):
            title = layer.get("annotations", {}).get(TITLE)
            if not title or "/" in title or title.startswith("."):
                continue
            data = self._get(host, name, f"blobs/{layer['digest']}").content
            algo, _, want = str(layer["digest"]).partition(":")
            if algo != "sha256" or hashlib.sha256(data).hexdigest() != want:
                raise ValueError(f"{title}: Prüfsumme passt nicht zu {layer['digest']}")
            (tmp / title).write_bytes(data)
        if not (tmp / "package.json").exists():
            raise ValueError(f"{host}/{name}: Artefakt enthält kein package.json")
        tmp.rename(out)


class PackageSource:
    """Hält das aktuelle Paket; wandernde Tags werden alle ``refresh_h`` Stunden neu aufgelöst."""

    def __init__(self, ref: str, cache: Path, client: httpx.Client, refresh_h: float) -> None:
        self.ref = ref
        self.cache = cache / "models"
        self.oci = OciClient(client)
        self.refresh_s = refresh_h * 3600
        self.current: ModelPackage | None = None
        self._checked = 0.0

    def get(self) -> ModelPackage:
        parsed = parse_ref(self.ref)
        if parsed is None:
            if self.current is None:
                self.current = load_package(Path(self.ref))
            return self.current
        if self.current is not None and time.monotonic() - self._checked < self.refresh_s:
            return self.current
        host, name, tag = parsed
        try:
            digest, manifest = self.oci.resolve(host, name, tag)
        except httpx.HTTPError as e:
            if self.current is not None:
                log.warning("package.refresh_failed", ref=self.ref, error=str(e))
                return self.current
            cached = self._newest_cached()
            if cached is None:
                raise
            log.warning("package.offline", ref=self.ref, using=str(cached), error=str(e))
            self.current = load_package(cached, self.ref)
            return self.current
        self._checked = time.monotonic()
        if self.current is not None and self.current.digest == digest:
            return self.current
        target = self.cache / digest.replace(":", "-")
        if not (target / "package.json").exists():
            self.oci.pull(host, name, manifest, target)
            log.info("package.pulled", ref=self.ref, digest=digest)
        self.current = load_package(target, self.ref, digest)
        return self.current

    def _newest_cached(self) -> Path | None:
        if not self.cache.exists():
            return None
        dirs = [d for d in self.cache.iterdir() if (d / "package.json").exists()]
        return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None
