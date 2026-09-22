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
    api = MagicMock()
    api.things = {}
    api.get_status.return_value = {}
    # __init__.py and config_flow.py each import the class by name.
    with patch("custom_components.jcihitachi_tw.JciHitachiAWSAPI", return_value=api), patch(
        "custom_components.jcihitachi_tw.config_flow.JciHitachiAWSAPI", return_value=api
    ):
        yield api
