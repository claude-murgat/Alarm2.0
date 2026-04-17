"""
Configuration pytest partagee.

Marqueur custom :
  @pytest.mark.failover  — tests qui manipulent Docker (stop/start containers)

Option CLI :
  --skip-failover        — exclut les tests failover (cycle dev rapide, ~6 min au lieu de 12)
  --only-failover        — lance uniquement les tests failover

Usage :
  python -m pytest tests/ -v                    # tout (~12 min)
  python -m pytest tests/ -v --skip-failover    # sans failover (~6 min)
  python -m pytest tests/ -v --only-failover    # failover seul (~5 min)
"""

import pytest


def pytest_addoption(parser):
    parser.addoption("--skip-failover", action="store_true", default=False,
                     help="Skip failover tests (docker stop/start)")
    parser.addoption("--only-failover", action="store_true", default=False,
                     help="Run only failover tests")


def pytest_configure(config):
    config.addinivalue_line("markers", "failover: tests that manipulate Docker containers")
    # Niveaux CI (voir docs/AI_STRATEGY.md) — declares pour eviter PytestUnknownMarkWarning
    config.addinivalue_line("markers", "unit: tier 1 — logique pure, pas de DB/HTTP")
    config.addinivalue_line("markers", "integration: tier 2 — FastAPI TestClient + SQLite temp")
    config.addinivalue_line("markers", "e2e: tier 3 — end-to-end contre cluster live")
    config.addinivalue_line("markers", "chaos: tier 4 — scenarios de panne (nightly, non-blocking)")


def pytest_collection_modifyitems(config, items):
    skip_fo = config.getoption("--skip-failover")
    only_fo = config.getoption("--only-failover")

    if not skip_fo and not only_fo:
        return

    skip_marker = pytest.mark.skip(reason="excluded by --skip-failover / --only-failover")
    for item in items:
        is_failover = any(mark.name == "failover" for mark in item.iter_markers())
        if skip_fo and is_failover:
            item.add_marker(skip_marker)
        elif only_fo and not is_failover:
            item.add_marker(skip_marker)
