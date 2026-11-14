# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from bambu_lan_cli.config import Config


@pytest.fixture
def config():
    return Config("192.168.1.100", "01P00A000000000", "00000000")


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    import socket

    def denied(*args, **kwargs):
        raise AssertionError("Unit tests must never contact hardware")

    monkeypatch.setattr(socket, "create_connection", denied)
