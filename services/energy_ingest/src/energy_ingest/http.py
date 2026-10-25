"""HTTP-Abruf mit Wiederholung bei Netzfehlern, 429 und 5xx, Drosselung je Host."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from predictiondb_core.log import get_logger

log = get_logger(__name__)

#: Abfrageparameter; als Liste von Paaren, wenn ein Name mehrfach vorkommt
Params = dict[str, Any] | list[tuple[str, Any]]

#: Mindestabstand zwischen zwei Abrufen je Host in Sekunden. Energy-Charts antwortet bei vielen
#: Abrufen nacheinander (Nachbarzonen, Kaltstart nach dem Deploy) sonst mit 429; AGSI+ sperrt
#: ab 60 Abrufen je Minute.
PACE_S: dict[str, float] = {"api.energy-charts.info": 2.0, "agsi.gie.eu": 1.1}
#: Obergrenze für eine vom Server per Retry-After verlangte Wartezeit
RETRY_AFTER_MAX_S = 120.0


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == httpx.codes.TOO_MANY_REQUESTS or code >= httpx.codes.INTERNAL_SERVER_ERROR
    return False


def retry_after(response: httpx.Response) -> float | None:
    """Wartezeit aus dem Header Retry-After (Sekunden oder HTTP-Datum), sonst ``None``."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class Http:
    """Dünne Hülle um ``httpx.Client`` mit Backoff; gemeinsam für alle Quellen.

    Vor jedem Abruf wird der Mindestabstand des Hosts (``pace``) eingehalten. Verlangt der
    Server per Retry-After eine Pause, wartet die Wiederholung mindestens so lange (höchstens
    ``retry_after_max``).
    """

    def __init__(
        self,
        client: httpx.Client,
        *,
        attempts: int = 4,
        wait_max: float = 30.0,
        pace: Mapping[str, float] | None = None,
        retry_after_max: float = RETRY_AFTER_MAX_S,
    ) -> None:
        self.client = client
        self.pace = dict(PACE_S if pace is None else pace)
        self._retry_after_max = retry_after_max
        self._last_call: dict[str, float] = {}
        self._backoff = wait_exponential_jitter(initial=1, max=wait_max)
        self._get = retry(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(attempts),
            wait=self._wait,
            reraise=True,
        )(self._get_once)

    def _wait(self, state: RetryCallState) -> float:
        wait = self._backoff(state)
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, httpx.HTTPStatusError):
            asked = retry_after(exc.response)
            if asked is not None:
                wait = max(wait, min(asked, self._retry_after_max))
            log.info(
                "http.retry",
                host=exc.request.url.host,
                status=exc.response.status_code,
                wait_s=round(wait, 1),
            )
        return wait

    def _throttle(self, url: str) -> None:
        host = httpx.URL(url).host
        gap = self.pace.get(host, 0.0)
        if gap <= 0:
            return
        wait = self._last_call.get(host, float("-inf")) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_call[host] = time.monotonic()

    def _get_once(
        self, url: str, params: Params | None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        self._throttle(url)
        r = self.client.get(url, params=params, headers=headers)
        if r.status_code == httpx.codes.TOO_MANY_REQUESTS or r.status_code >= 500:
            r.raise_for_status()
        return r

    def get(
        self,
        url: str,
        params: Params | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET mit Wiederholung; andere 4xx-Antworten gehen ohne Ausnahme zurück."""
        return self._get(url, params, headers)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        r = self.get(url, params)
        r.raise_for_status()
        return r.json()
