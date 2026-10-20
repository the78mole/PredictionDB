import pytest
from obstore.store import MemoryStore

from wx_ingest.bronze import Bronze


@pytest.fixture
def bronze() -> Bronze:
    return Bronze(MemoryStore())
