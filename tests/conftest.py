from pathlib import Path

import pytest


@pytest.fixture
def fixture_csv() -> str:
    return (Path(__file__).parent / "fixtures" / "transactions.csv").read_text()
