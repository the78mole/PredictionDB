import pytest
from gribkit.testing import make_global_message


@pytest.fixture
def global_message():
    return make_global_message()
