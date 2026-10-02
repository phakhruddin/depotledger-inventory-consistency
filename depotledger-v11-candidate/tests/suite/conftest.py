from __future__ import annotations

from pathlib import Path

import pytest

from .tools.scoring import load_obligations
from .tools.trial import TrialSession

SPEC = load_obligations(Path(__file__).with_name("obligations.yaml"))
# Obligations run in the order they are declared: setup-ish checks first,
# faults last, destruction last of all.
ORDER = {item["id"]: index for index, item in enumerate(SPEC["obligations"])}


@pytest.fixture(scope="session")
def trial():
    session = TrialSession()
    try:
        yield session
    finally:
        # Always emit a report, even when the run aborted part way.
        session.finish()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    items.sort(key=lambda item: ORDER.get(getattr(item.obj, "obligation_id", ""), len(ORDER)))
