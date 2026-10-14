import json
from pathlib import Path

import pytest

from mfp_connector import db

FIXTURES = Path(__file__).parent / "fixtures"
SPIKE_OUT = Path(__file__).parent.parent / "spike-out"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()
