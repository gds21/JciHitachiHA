"""Tests for the user and reauth config flows."""
import httpx
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICES, CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jcihitachi_tw.const import (CONF_ADD_ANOTHER_DEVICE,
                                                   CONF_RETRY, DOMAIN)

USER_INPUT = {
    CONF_EMAIL: "user@example.com",
    CONF_PASSWORD: "secret",
    CONF_RETRY: 5,
    CONF_DEVICES: "",
    CONF_ADD_ANOTHER_DEVICE: False,
}


async def _start_user_flow(hass, user_input):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=user_input
    )


async def test_user_flow_cannot_connect(hass, mock_api):
    mock_api.login.side_effect = httpx.ConnectError("dns")
    result = await _start_user_flow(hass, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    mock_api.logout.assert_called()


async def test_user_flow_bad_password(hass, mock_api):
    mock_api.login.side_effect = RuntimeError(
        "An error occurred when signing into AWS Cognito Service: "
        "NotAuthorizedException Incorrect username or password."
    )
    result = await _start_user_flow(hass, USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "login_error"}


async def test_user_flow_success_and_duplicate(hass, mock_api):
    result = await _start_user_flow(hass, USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {DOMAIN: USER_INPUT}
    # The flow must not leave a logged-in API in hass.data; the entry setup
    # (mocked here) logs in on its own.
    mock_api.logout.assert_called()
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth_flow_updates_password_and_reloads(hass, mock_api):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="JciHitachi TW",
        data={DOMAIN: {**USER_INPUT, CONF_DEVICES: [], CONF_PASSWORD: "old"}},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    mock_api.login.side_effect = [RuntimeError(
        "An error occurred when signing into AWS Cognito Service: "
        "NotAuthorizedException Incorrect username or password."
    ), None, None]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "still-wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "login_error"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_PASSWORD: "new"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[DOMAIN][CONF_PASSWORD] == "new"
    assert entry.state is config_entries.ConfigEntryState.LOADED
