"""Tests for per-device availability, command failures and auto-reload."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICES, CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (MockConfigEntry,
                                                          async_fire_time_changed)

from custom_components.jcihitachi_tw import RELOAD_AFTER_FAILURES
from custom_components.jcihitachi_tw.const import CONF_RETRY, DOMAIN

ENTRY_DATA = {DOMAIN: {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret", CONF_RETRY: 5, CONF_DEVICES: []}}
LIVING = "living"
BEDROOM = "bedroom"
DEVICE_TIMEOUT = RuntimeError(
    f"Timed out refreshing {BEDROOM} status code. Please ensure the device is online and avoid opening the official app."
)
SESSION_ERROR = RuntimeError("An error occurred when signing into AWS Cognito Service: InternalErrorException")


def _thing(name, mac):
    # type "HE" only creates one indoor-temperature sensor and one fan entity.
    return SimpleNamespace(name=name, type="HE", gateway_mac_address=mac, brand="Hitachi",
                           model="test", firmware_version="1", available=True, monthly_data=None)


@pytest.fixture
def two_devices(mock_api):
    """Two devices, sensor platform only, temperatures 25 and 26."""
    mock_api.things = {LIVING: _thing(LIVING, "aa"), BEDROOM: _thing(BEDROOM, "bb")}
    mock_api.get_status.return_value = {
        LIVING: SimpleNamespace(IndoorTemperature=25, status={}),
        BEDROOM: SimpleNamespace(IndoorTemperature=26, status={}),
    }
    with patch("custom_components.jcihitachi_tw.PLATFORMS", ["sensor"]):
        yield mock_api


async def _setup(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="JciHitachi TW", data=ENTRY_DATA)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _poll(hass, times=1):
    """Fire the coordinator's 30 s timer and wait for the refresh.

    Scheduled refreshes run as config-entry background tasks, which
    async_block_till_done() skips unless asked to wait for them.
    """
    for _ in range(times):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done(wait_background_tasks=True)


LIVING_SENSOR = f"sensor.{LIVING}_indoor_temperature"
BEDROOM_SENSOR = f"sensor.{BEDROOM}_indoor_temperature"


async def test_one_offline_device_only_marks_that_device_unavailable(hass, two_devices):
    await _setup(hass)
    assert hass.states.get(LIVING_SENSOR).state == "25"
    assert hass.states.get(BEDROOM_SENSOR).state == "26"

    def refresh(device_name=None, **kwargs):
        if device_name == BEDROOM:
            raise DEVICE_TIMEOUT

    two_devices.refresh_status.side_effect = refresh
    await _poll(hass)

    assert hass.states.get(LIVING_SENSOR).state == "25"
    assert hass.states.get(BEDROOM_SENSOR).state == "unavailable"
    # every device is refreshed on its own so the living room is not hidden
    assert {c.kwargs.get("device_name") for c in two_devices.refresh_status.call_args_list} == {LIVING, BEDROOM}

    two_devices.refresh_status.side_effect = None
    await _poll(hass)
    assert hass.states.get(BEDROOM_SENSOR).state == "26"


async def test_session_error_marks_all_unavailable_and_reloads_after_repeated_failures(hass, two_devices):
    entry = await _setup(hass)
    two_devices.refresh_status.side_effect = SESSION_ERROR

    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        await _poll(hass)
        assert hass.states.get(LIVING_SENSOR).state == "unavailable"
        assert hass.states.get(BEDROOM_SENSOR).state == "unavailable"
        assert reload.call_count == 0

        await _poll(hass, RELOAD_AFTER_FAILURES - 1)
        reload.assert_awaited_once_with(entry.entry_id)

        # only scheduled once, even if failures continue
        await _poll(hass, 2)
        assert reload.call_count == 1


async def test_all_devices_timing_out_counts_as_session_failure(hass, two_devices):
    await _setup(hass)
    two_devices.refresh_status.side_effect = DEVICE_TIMEOUT
    await _poll(hass)
    assert hass.states.get(LIVING_SENSOR).state == "unavailable"
    assert hass.states.get(BEDROOM_SENSOR).state == "unavailable"


async def test_unconfirmed_command_raises(hass, two_devices):
    await _setup(hass)
    entity = hass.data["entity_components"]["sensor"].get_entity(LIVING_SENSOR)
    assert entity is not None

    two_devices.set_status.return_value = False
    entity.put_queue(status_name="power", status_str_value="off")
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await hass.async_add_executor_job(entity.update)
    two_devices.set_status.assert_called_once_with(
        status_name="power", device_name=LIVING, status_value=None, status_str_value="off"
    )

    two_devices.set_status.reset_mock()
    two_devices.set_status.return_value = True
    entity.put_queue(status_name="power", status_str_value="off")
    await hass.async_add_executor_job(entity.update)  # no exception
    two_devices.set_status.assert_called_once()
