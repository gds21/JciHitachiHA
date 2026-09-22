"""Shared fixtures."""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


@pytest.fixture
def mock_api():
    """Patch JciHitachiAWSAPI with a MagicMock instance that logs in fine."""
    with patch("custom_components.jcihitachi_tw.JciHitachiAWSAPI") as cls:
        api = MagicMock()
        api.things = {}
        api.get_status.return_value = {}
        cls.return_value = api
        yield api
