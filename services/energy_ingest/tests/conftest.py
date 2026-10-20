from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from energy_ingest.http import Http

FIXTURES = Path(__file__).parent / "fixtures"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_http() -> Iterator[Callable[[Handler], Http]]:
    clients: list[httpx.Client] = []

    def make(handler: Handler) -> Http:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return Http(client, attempts=2, wait_max=0.01)

    yield make
    for c in clients:
        c.close()
