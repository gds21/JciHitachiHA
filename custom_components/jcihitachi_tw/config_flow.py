"""JciHitachi integration."""
import asyncio
import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICES, CONF_EMAIL, CONF_PASSWORD
from JciHitachi.api import JciHitachiAWSAPI

from . import LOGIN_TIMEOUT, TRANSIENT_ERRORS, async_logout, is_auth_error
from .const import (CONF_ADD_ANOTHER_DEVICE, CONF_RETRY,
                    CONFIG_FLOW_ADD_DEVICE_SCHEMA, CONFIG_FLOW_SCHEMA,
                    DEFAULT_RETRY, DOMAIN)

_LOGGER = logging.getLogger(__name__)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def validate_auth(hass, email, password, device_names, max_retries) -> None:
    """Validates JciHitachiAWS account and devices.

    The API instance is discarded afterwards; ``async_setup_entry`` performs
    its own login so that reload/reauth always start from a fresh connection.
    """

    device_names_ = None if device_names == [] else device_names

    api = JciHitachiAWSAPI(
        email=email,
        password=password,
        device_names=device_names_,
        max_retries=max_retries,
    )
    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            await hass.async_add_executor_job(api.login)
    finally:
        await async_logout(hass, api)


def _map_login_error(err: BaseException) -> str:
    """Map a login exception to a translation key in ``errors.base``."""
    if isinstance(err, AssertionError):
        _LOGGER.error(f"Assertion check error: {err}")
        return "assertion_check_error"
    if isinstance(err, RuntimeError):
        _LOGGER.error(f"Failed to login API: {err}")
        return "login_error" if is_auth_error(err) else "unknown_error"
    if isinstance(err, TRANSIENT_ERRORS):
        _LOGGER.error(f"Cannot connect to API: {err}")
        return "cannot_connect"
    _LOGGER.error(f"Failed to login API: {err}")
    return "unknown_error"


class JciHitachiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """JciHitachi config flow."""

    VERSION = 1

    def __init__(self):
        """Initialize the config flow."""
        self.data = None

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_EMAIL].strip().lower())
            self._abort_if_unique_id_configured()

            try:
                await validate_auth(
                    self.hass,
                    user_input[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_DEVICES],
                    user_input[CONF_RETRY]
                )
            except Exception as err:  # noqa: BLE001
                errors['base'] = _map_login_error(err)

            if not errors:
                return self.async_create_entry(
                    title="JciHitachi TW",
                    data={
                        DOMAIN: user_input
                    }
                )
        return self.async_show_form(
            step_id="user", data_schema=CONFIG_FLOW_SCHEMA, errors=errors
        )

    async def async_step_add_device(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input[CONF_DEVICES] != "":
                self.data[CONF_DEVICES].append(user_input[CONF_DEVICES])
            if user_input[CONF_ADD_ANOTHER_DEVICE]:
                return await self.async_step_add_device()
            else:
                self.data[CONF_ADD_ANOTHER_DEVICE] = False
                return await self.async_step_user(self.data)

        return self.async_show_form(
            step_id="add_device", data_schema=CONFIG_FLOW_ADD_DEVICE_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """Handle re-authentication requested by ConfigEntryAuthFailed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Ask for a new password and reload the entry."""
        errors = {}
        entry = self._get_reauth_entry()
        old = entry.data[DOMAIN]

        if user_input is not None:
            try:
                await validate_auth(
                    self.hass,
                    old[CONF_EMAIL],
                    user_input[CONF_PASSWORD],
                    old.get(CONF_DEVICES) or [],
                    old.get(CONF_RETRY, DEFAULT_RETRY),
                )
            except Exception as err:  # noqa: BLE001
                errors['base'] = _map_login_error(err)

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={DOMAIN: {**old, CONF_PASSWORD: user_input[CONF_PASSWORD]}},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"email": old[CONF_EMAIL]},
            errors=errors,
        )
