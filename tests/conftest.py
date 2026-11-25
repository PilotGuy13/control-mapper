"""Shared test fixtures.

Streamlit finds .streamlit/config.toml only in the process's working directory, so the
app's loopback guard would stop every UI test when pytest runs from anywhere other
than the repo root. Pin server.address to loopback for every test, and restore the
previous value afterwards. Tests that exercise the guard override it explicitly
(test_app_ui.server_address); that fixture runs after this autouse one and restores
the loopback value on teardown, so their overrides are not defeated.
test_security_phase1.test_streamlit_config_binds_loopback_only still pins the file.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _loopback_server_address():
    from streamlit import config as st_config

    old = st_config.get_option("server.address")
    st_config.set_option("server.address", "127.0.0.1")
    yield
    st_config.set_option("server.address", old)
