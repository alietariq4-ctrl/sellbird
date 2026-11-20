"""pytest configuration: register the opt-in ``network`` marker.

The offline suite never touches the network. The few checks that must see a live
vendor API carry ``@pytest.mark.network`` and are deselected by default, so
``pytest harvey_brain/tests`` needs no flags and no keys.
"""

from __future__ import annotations

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: needs a live vendor API; deselected unless -m network is given",
    )


def pytest_collection_modifyitems(config, items):
    """Skip network tests unless the operator asked for them with -m network."""
    selected = config.getoption("markexpr", "") or ""
    if "network" in selected:
        return
    skip = pytest.mark.skip(reason="network test: run with -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)
