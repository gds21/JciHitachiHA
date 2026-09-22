"""Tests for config entry setup, retry classification, unload and reload."""
import httpx
import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_DEVICES, CONF_EMAIL, CONF_PASSWORD
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jcihitachi_tw.const import CONF_RETRY, DOMAIN

ENTRY_DATA = {
    DOMAIN: {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_RETRY: 5,
        CONF_DEVICES: [],
    }
}


def _entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="JciHitachi TW", data=ENTRY_DATA)


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _reauth_flows(hass, entry):
    return [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"].get("source") == SOURCE_REAUTH
        and f["context"].get("entry_id") == entry.entry_id
    ]


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("[Errno -3] Try again"),
        httpx.ConnectTimeout("timed out"),
        RuntimeError("An error occurred when connecting to MQTT endpoint."),
        RuntimeError("Timed out refreshing 客廳. Please ensure the device is online."),
        KeyError("AuthenticationResult"),
        ValueError("something unexpected"),
    ],
)
async def test_transient_login_error_sets_retry(hass, mock_api, exc):
    """Network/DNS/cloud errors at boot must yield setup_retry, not setup_error."""
    mock_api.login.side_effect = exc
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert DOMAIN not in hass.data
    assert not _reauth_flows(hass, entry)


async def test_bad_credentials_start_reauth(hass, mock_api):
    """Cognito NotAuthorizedException -> setup_error + reauth flow."""
    mock_api.login.side_effect = RuntimeError(
        "An error occurred when signing into AWS Cognito Service: "
        "NotAuthorizedException Incorrect username or password."
    )
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass, entry)) == 1


async def test_unknown_device_name_is_permanent_error(hass, mock_api):
    """A wrong device name in the config is not retried and needs no reauth."""
    mock_api.login.side_effect = AssertionError(
        "Some of device_names are not available from the API."
    )
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not _reauth_flows(hass, entry)


async def test_setup_unload_reload(hass, mock_api):
    """Happy path, then unload cleans up, then reload logs in again."""
    entry = _entry()
    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert DOMAIN in hass.data
    assert mock_api.login.call_count == 1
    assert entry.supports_unload

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert DOMAIN not in hass.data
    mock_api.logout.assert_called_once()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert mock_api.login.call_count == 2
