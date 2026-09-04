# Disable broken third-party pytest plugins that have dependency conflicts
collect_ignore_glob = []

import sys

# Prevent anchorpy and web3 ethereum test plugins from loading
# (they have broken transitive dependencies: missing pytest_asyncio, ContractName, etc.)
pytest_plugins = []


def pytest_configure(config):
    """Disable broken plugins before they crash pytest startup."""
    for name in ("pytest_ethereum", "pytest_anchorpy"):
        try:
            config.pluginmanager.set_blocked(name)
        except Exception:
            pass
