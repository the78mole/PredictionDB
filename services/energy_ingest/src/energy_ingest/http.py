"""HTTP-Abruf mit Wiederholung bei Netzfehlern, 429 und 5xx."""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from predictiondb_core.log import get_logger

log = get_logger(__name__)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == httpx.codes.TOO_MANY_REQUESTS or code >= httpx.codes.INTERNAL_SERVER_ERROR
    return False


class Http:
    """Dünne Hülle um ``httpx.Client`` mit Backoff; gemeinsam für alle Quellen."""

    def __init__(self, client: httpx.Client, *, attempts: int = 4, wait_max: float = 30.0) -> None:
        self.client = client
        self._get = retry(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(attempts),
            wait=wait_exponential_jitter(initial=1, max=wait_max),
            reraise=True,
        )(self._get_once)

    def _get_once(
        self, url: str, params: dict[str, Any] | None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        r = self.client.get(url, params=params, headers=headers)
        if r.status_code == httpx.codes.TOO_MANY_REQUESTS or r.status_code >= 500:
            r.raise_for_status()
        return r

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET mit Wiederholung; andere 4xx-Antworten gehen ohne Ausnahme zurück."""
        return self._get(url, params, headers)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = self.get(url, params)
        r.raise_for_status()
        return r.json()
