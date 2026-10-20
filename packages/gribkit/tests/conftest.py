from typing import Any

import pytest

from gribkit.testing import make_global_message


@pytest.fixture
def global_message() -> tuple[bytes, Any, Any]:
    return make_global_message()
