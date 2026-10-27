"""Hilfen für Tests: simulierte OCI-Registry (GHCR) für ``httpx.MockTransport``."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from wx_exchange.oci import digest


@dataclass
class FakeRegistry:
    """Ein Repository mit Bearer-Anmeldung; Schreiben nur mit Passwort ``secret``."""

    host: str = "registry.test"
    repository: str = "owner/data/silver"
    blobs: dict[str, bytes] = field(default_factory=dict)
    manifests: dict[str, bytes] = field(default_factory=dict)
    uploads: dict[str, bool] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    page_size: int = 1000
    #: so viele Blob-Uploads scheitern mit 500 (wie gelegentlich bei GHCR)
    fail_uploads: int = 0

    def manifest(self, tag: str) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.manifests[tag])
        return data

    def blob_puts(self) -> int:
        puts = (r for r in self.requests if r.method == "PUT")
        return sum(1 for r in puts if "/blobs/uploads/" in r.url.path)

    def manifest_puts(self) -> int:
        return sum(1 for r in self.requests if r.method == "PUT" and "/manifests/" in r.url.path)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/token":
            write = request.headers.get("authorization", "").startswith("Basic ")
            return httpx.Response(200, json={"token": "rw" if write else "ro"})
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return httpx.Response(
                401,
                headers={
                    "www-authenticate": f'Bearer realm="https://{self.host}/token",'
                    f'service="{self.host}",scope="repository:{self.repository}:pull"'
                },
            )
        writable = auth == "Bearer rw"
        base = f"/v2/{self.repository}"
        if not path.startswith(base):
            return httpx.Response(404)
        rest = path[len(base) :]
        if rest == "/tags/list":
            tags = sorted(self.manifests)
            last = request.url.params.get("last")
            if last:
                tags = [t for t in tags if t > last]
            page = tags[: self.page_size]
            headers = {}
            if len(tags) > self.page_size:
                nxt = f"{base}/tags/list?n={self.page_size}&last={page[-1]}"
                headers["link"] = f'<{nxt}>; rel="next"'
            listing = {"name": self.repository, "tags": page}
            return httpx.Response(200, json=listing, headers=headers)
        m = re.fullmatch(r"/manifests/(.+)", rest)
        if m:
            if request.method == "GET":
                if m[1] not in self.manifests:
                    return httpx.Response(404)
                return httpx.Response(200, content=self.manifests[m[1]])
            if request.method == "PUT" and writable:
                body = request.content
                for d in json.loads(body)["layers"]:
                    if d["digest"] not in self.blobs:
                        return httpx.Response(400, text="blob unknown")
                self.manifests[m[1]] = body
                return httpx.Response(201)
            return httpx.Response(403)
        m = re.fullmatch(r"/blobs/(sha256:[0-9a-f]+)", rest)
        if m:
            if m[1] not in self.blobs:
                return httpx.Response(404)
            if request.method == "HEAD":
                return httpx.Response(200)
            return httpx.Response(200, content=self.blobs[m[1]])
        if rest == "/blobs/uploads/" and request.method == "POST" and writable:
            uid = uuid.uuid4().hex
            self.uploads[uid] = True
            return httpx.Response(202, headers={"location": f"{base}/blobs/uploads/{uid}"})
        m = re.fullmatch(r"/blobs/uploads/([0-9a-f]+)", rest)
        if m and request.method == "PUT" and writable and self.uploads.pop(m[1], False):
            if self.fail_uploads:
                self.fail_uploads -= 1
                return httpx.Response(500, json={"errors": [{"code": "UNKNOWN"}]})
            d = request.url.params["digest"]
            if digest(request.content) != d:
                return httpx.Response(400, text="digest invalid")
            self.blobs[d] = request.content
            return httpx.Response(201)
        return httpx.Response(403 if not writable else 404)
