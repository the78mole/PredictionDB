"""Kleine HTTP-Schnittstelle (nur lesend) über den Dateien in ``out/``.

=================================== ===========================================================
``GET /health``                     200, wenn eine frische Prognose vorliegt, sonst 503
``GET /v1/forecast``                ``latest.json``; ``?target=da_price_de_lu`` nur ein Ziel,
                                    ``?format=csv`` als Tabelle
``GET /v1/forecast/<ziel>``         wie ``?target=<ziel>``
``GET /v1/model``                   Modellpaket: Version, Variante, Trainingszeitraum, Kennzahlen
=================================== ===========================================================

Die Schnittstelle liest bei jeder Anfrage ``latest.json``; der Prognoselauf ersetzt die Datei
atomar. So bleiben Datei und HTTP immer gleich, und ein Neustart verliert nichts.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pdb_predict.service import to_csv


class Api:
    def __init__(self, out: Path, stale_after_h: float) -> None:
        self.out = out
        self.stale = timedelta(hours=stale_after_h)
        self.last_error: str | None = None

    def latest(self) -> dict[str, Any] | None:
        p = self.out / "latest.json"
        if not p.exists():
            return None
        doc: dict[str, Any] = json.loads(p.read_text())
        return doc

    def handle(self, path: str) -> tuple[int, str, bytes]:
        url = urlparse(path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        parts = [p for p in url.path.split("/") if p]
        doc = self.latest()
        if parts == ["health"]:
            return self._health(doc)
        if parts == []:
            return _json(200, {"endpoints": ["/health", "/v1/forecast", "/v1/model"]})
        if doc is None:
            return _json(503, {"error": "noch keine Prognose", "last_error": self.last_error})
        if parts == ["v1", "model"]:
            return _json(200, doc["model"])
        if parts[:2] == ["v1", "forecast"] and len(parts) <= 3:
            target = parts[2] if len(parts) == 3 else q.get("target")
            if target and target not in doc["targets"]:
                return _json(
                    404, {"error": f"unbekanntes Ziel {target!r}", "targets": list(doc["targets"])}
                )
            if q.get("format") == "csv":
                return 200, "text/csv; charset=utf-8", to_csv(doc, target).encode()
            if target:
                doc = {**doc, "targets": {target: doc["targets"][target]}}
            return _json(200, doc)
        return _json(404, {"error": "unbekannter Pfad"})

    def _health(self, doc: dict[str, Any] | None) -> tuple[int, str, bytes]:
        if doc is None:
            return _json(503, {"status": "starting", "last_error": self.last_error})
        created = datetime.fromisoformat(doc["created"])
        fresh = datetime.now(UTC) - created < self.stale
        return _json(
            200 if fresh else 503,
            {
                "status": "ok" if fresh else "stale",
                "issue_time": doc["issue_time"],
                "created": doc["created"],
                "model": doc["model"].get("version"),
                "last_error": self.last_error,
            },
        )


def _json(status: int, body: Any) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode()


def serve(api: Api, host: str, port: int) -> ThreadingHTTPServer:
    """Startet den Server in einem Hintergrund-Thread und gibt ihn zurück."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            status, ctype, body = api.handle(self.path)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
