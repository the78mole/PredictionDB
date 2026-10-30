"""Schlanker Client für die OCI Distribution API (GHCR), nur was der Silber-Austausch braucht.

Kein ``oras``-Binary und kein Docker: Blobs werden monolithisch hochgeladen (ein Lauf sind
~13 MB), Manifeste als JSON geschrieben und gelesen. Die Anmeldung folgt dem Bearer-Ablauf der
Registry: Auf ``401`` holt der Client beim ``realm`` aus ``WWW-Authenticate`` ein Token, mit
Benutzer und Passwort (``GITHUB_TOKEN``) oder anonym für öffentliche Pakete.

@relation(SWA-WXX-OCI, scope=file)
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
#: leere Konfiguration für Artefakte (OCI image-spec 1.1)
EMPTY = {
    "mediaType": "application/vnd.oci.empty.v1+json",
    "digest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "size": 2,
}


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class RegistryError(RuntimeError):
    pass


class ServerError(RegistryError):
    """5xx der Registry (GHCR meldet gelegentlich ``500 unknown error``); wird wiederholt."""


class AccessDenied(RegistryError):
    """Kein Token für das Repository; anonym heißt das bei GHCR meist: gibt es (noch) nicht."""


@dataclass
class Registry:
    """Ein Repository in einer Registry, z. B. ``ghcr.io`` + ``the78mole/predictiondb-data/…``."""

    host: str
    repository: str
    client: httpx.Client
    username: str | None = None
    password: str | None = None
    #: Wartezeiten zwischen Wiederholungen bei 5xx oder Verbindungsfehlern (Sekunden)
    retry_waits: tuple[float, ...] = (2.0, 8.0, 30.0)
    _token: str | None = field(default=None, repr=False)

    @property
    def base(self) -> str:
        return f"https://{self.host}/v2/{self.repository}"

    # --- Anmeldung ---------------------------------------------------------------------------

    def _authenticate(self, challenge: str) -> None:
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.pop("realm", None)
        if not challenge.lower().startswith("bearer") or realm is None:
            raise RegistryError(f"unbekannte Anmeldung: {challenge}")
        if self.password and "scope" in params:
            # Schreibrecht gleich mit anfordern, sonst gibt es nur ein Lese-Token
            params["scope"] = f"repository:{self.repository}:pull,push"
        auth = (self.username or "token", self.password) if self.password else None
        r = self.client.get(realm, params=params, auth=auth)
        if r.status_code in (401, 403):
            raise AccessDenied(f"Token abgelehnt ({r.status_code}): {r.text[:200]}")
        if r.status_code != 200:
            raise RegistryError(f"Token abgelehnt ({r.status_code}): {r.text[:200]}")
        body = r.json()
        self._token = body.get("token") or body.get("access_token")

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """@relation(SWR-WXX-001, scope=function)"""
        headers = dict(kwargs.pop("headers", {}))
        for _ in range(2):
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            r = self.client.request(method, url, headers=headers, **kwargs)
            if r.status_code != 401:
                return r
            self._authenticate(r.headers.get("www-authenticate", ""))
        return r

    @staticmethod
    def _check(r: httpx.Response, *ok: int) -> httpx.Response:
        if r.status_code not in ok:
            req = r.request
            error = ServerError if r.status_code >= 500 else RegistryError
            raise error(f"{req.method} {req.url} → {r.status_code}: {r.text[:300]}")
        return r

    def _retry[T](self, fn: Callable[[], T]) -> T:
        """Wiederholt einen ganzen Vorgang (beim Upload samt neuer Upload-Sitzung).

        @relation(SWR-WXX-001, scope=function)
        """
        for wait in self.retry_waits:
            try:
                return fn()
            except (ServerError, httpx.TransportError):
                time.sleep(wait)
        return fn()

    # --- Lesen -------------------------------------------------------------------------------

    def tags(self) -> list[str]:
        """@relation(SWR-WXX-001, scope=function)"""
        out: list[str] = []
        url: str | None = f"{self.base}/tags/list?n=1000"
        while url:
            try:
                r = self._request("GET", url)
            except AccessDenied:
                if self.password:
                    raise
                return []  # anonym: Repository gibt es noch nicht
            if r.status_code == 404:  # Repository gibt es noch nicht
                return []
            self._check(r, 200)
            out.extend(r.json().get("tags") or [])
            link = re.search(r"<([^>]+)>;\s*rel=\"next\"", r.headers.get("link", ""))
            url = None
            if link:
                nxt = link[1]
                url = nxt if nxt.startswith("http") else f"https://{self.host}{nxt}"
        return out

    def manifest(self, tag: str) -> dict[str, Any] | None:
        try:
            r = self._request("GET", f"{self.base}/manifests/{tag}", headers={"Accept": MANIFEST})
        except AccessDenied:
            if self.password:
                raise
            return None
        if r.status_code == 404:
            return None
        data: dict[str, Any] = self._check(r, 200).json()
        return data

    def blob(self, digest_: str) -> bytes:
        def get() -> bytes:
            r = self._request("GET", f"{self.base}/blobs/{digest_}", follow_redirects=True)
            return self._check(r, 200).content

        data = self._retry(get)
        if digest(data) != digest_:
            raise RegistryError(f"Blob {digest_}: Prüfsumme stimmt nicht")
        return data

    def has_blob(self, digest_: str) -> bool:
        r = self._request("HEAD", f"{self.base}/blobs/{digest_}", follow_redirects=True)
        return r.status_code == 200

    # --- Schreiben ---------------------------------------------------------------------------

    def push_blob(self, data: bytes) -> str:
        """@relation(SWR-WXX-001, scope=function)"""
        return self._retry(lambda: self._push_blob(data))

    def _push_blob(self, data: bytes) -> str:
        d = digest(data)
        if self.has_blob(d):
            return d
        r = self._check(self._request("POST", f"{self.base}/blobs/uploads/"), 202)
        location = r.headers["location"]
        if not location.startswith("http"):
            location = f"https://{self.host}{location}"
        sep = "&" if "?" in location else "?"
        self._check(
            self._request(
                "PUT",
                f"{location}{sep}digest={d}",
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            ),
            201,
        )
        return d

    def put_manifest(self, tag: str, manifest: dict[str, Any]) -> str:
        """@relation(SWR-WXX-001, scope=function)"""
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if manifest.get("config") == EMPTY:
            self.push_blob(b"{}")
        self._retry(
            lambda: self._check(
                self._request(
                    "PUT",
                    f"{self.base}/manifests/{tag}",
                    content=body,
                    headers={"Content-Type": MANIFEST},
                ),
                201,
            )
        )
        return digest(body)
