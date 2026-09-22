"""JciHitachi integration."""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from queue import Queue
from typing import Optional

import httpx
from homeassistant.exceptions import (ConfigEntryAuthFailed, ConfigEntryError,
                                      ConfigEntryNotReady)
from homeassistant.helpers import discovery
from homeassistant.helpers.update_coordinator import (CoordinatorEntity,
                                                      DataUpdateCoordinator,
                                                      UpdateFailed)
from JciHitachi import __version__
from JciHitachi.api import JciHitachiAWSAPI

from .const import (API, CONF_DEVICES, CONF_EMAIL, CONF_PASSWORD, CONF_RETRY,
                    CONFIG_SCHEMA, COORDINATOR, DOMAIN, UPDATE_DATA,
                    UPDATED_DATA)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "climate", "fan", "humidifier", "number", "sensor", "switch", "light"]
DATA_UPDATE_INTERVAL = timedelta(seconds=30)
BASE_TIMEOUT = 5
# awscrt's MQTT connect has no timeout of its own; never let login block setup forever.
LOGIN_TIMEOUT = 120

# Substrings of RuntimeError messages produced by LibJciHitachi that mean the
# credentials are wrong (aws_connection.py: "<__type> <message>" from Cognito,
# and "Invalid email or password" from the IoT API).
AUTH_ERROR_MARKERS = (
    "NotAuthorizedException",
    "UserNotFoundException",
    "UserNotConfirmedException",
    "PasswordResetRequiredException",
    "Invalid email or password",
)

# Errors that are transient (network/DNS not up yet at boot, cloud hiccup,
# non-JSON error page, unexpected response shape). Setup should be retried.
TRANSIENT_ERRORS = (httpx.HTTPError, TimeoutError, OSError, json.JSONDecodeError, KeyError)


def is_auth_error(err: BaseException) -> bool:
    """Return True if the library error indicates invalid credentials."""
    return isinstance(err, RuntimeError) and any(marker in str(err) for marker in AUTH_ERROR_MARKERS)


async def async_logout(hass, api) -> None:
    """Best-effort MQTT disconnect.

    ``api.logout()`` is not None-safe when login never reached the MQTT stage,
    so any error here is ignored.
    """
    try:
        await hass.async_add_executor_job(api.logout)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Ignoring error during logout: %s", err)


def build_coordinator(hass, api, config_entry=None):

    timeout = BASE_TIMEOUT + len(api.things) * 2

    async def async_update_data():
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            # Note: asyncio.TimeoutError and aiohttp.ClientError are already
            # handled by the data update coordinator.
            async with asyncio.timeout(timeout):
                await hass.async_add_executor_job(api.refresh_status)
                hass.data[DOMAIN][UPDATED_DATA] = api.get_status(legacy=True)

        except TimeoutError as err:
            raise UpdateFailed("Command executed timed out when regularly fetching data.") from err

        except Exception as err:
            raise UpdateFailed(f"Error communicating with API: {err}")
        
        _LOGGER.debug(
            f"Latest data: {[(name, value.status) for name, value in hass.data[DOMAIN][UPDATED_DATA].items()]}")

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        # Name of the data. For logging purposes.
        name=DOMAIN,
        config_entry=config_entry,
        update_method=async_update_data,
        # Polling interval. Will only be polled if there are subscribers.
        update_interval=DATA_UPDATE_INTERVAL,
    )

    # Reset the update scheduler as the data already exists in
    # `hass.data[DOMAIN][UPDATED_DATA]`.
    coordinator.async_set_updated_data(None)

    return coordinator

async def async_setup(hass, config):
    """Set up from the configuration.yaml"""
    if config.get(DOMAIN, None) is None:
        # skip if no config defined in configuration.yaml"""
        return True
    _LOGGER.debug(
        {
            "CONF_EMAIL": config[DOMAIN].get(CONF_EMAIL),
            "CONF_PASSWORD": '*' * len(config[DOMAIN].get(CONF_PASSWORD)),
            "CONF_RETRY": config[DOMAIN].get(CONF_RETRY),
            "CONF_DEVICES": config[DOMAIN].get(CONF_DEVICES)
        }
    )

    if config[DOMAIN].get(CONF_DEVICES) == []:
        config[DOMAIN][CONF_DEVICES] = None

    api = JciHitachiAWSAPI(
        email=config[DOMAIN].get(CONF_EMAIL),
        password=config[DOMAIN].get(CONF_PASSWORD),
        device_names=config[DOMAIN].get(CONF_DEVICES),
        max_retries=config[DOMAIN].get(CONF_RETRY),
    )

    try:
        await hass.async_add_executor_job(api.login)
    except AssertionError as err:
        _LOGGER.error(f"Assertion check error: {err}")
        return False
    except RuntimeError as err:
        _LOGGER.error(f"Failed to login API: {err}")
        return False

    _LOGGER.debug(f"Backend version: {__version__}")
    _LOGGER.debug(f"Thing info: {[thing for thing in api.things.values()]}")

    hass.data[DOMAIN] = {}
    hass.data[DOMAIN][API] = api
    hass.data[DOMAIN][UPDATE_DATA] = Queue()
    hass.data[DOMAIN][UPDATED_DATA] = api.get_status(legacy=True)
    hass.data[DOMAIN][COORDINATOR] = build_coordinator(hass, api, config_entry=None)

    # Start jcihitachi components
    _LOGGER.debug("Starting JciHitachi components.")
    for platform in PLATFORMS:
        discovery.load_platform(hass, platform, DOMAIN, {}, config)

    # Return boolean to indicate that initialization was successful.
    return True

async def async_setup_entry(hass, config_entry):
    """Set up from a config entry."""

    config = config_entry.data[DOMAIN]
    _LOGGER.debug(
        {
            "CONF_EMAIL": config.get(CONF_EMAIL),
            "CONF_PASSWORD": '*' * len(config.get(CONF_PASSWORD)),
            "CONF_RETRY": config.get(CONF_RETRY),
            "CONF_DEVICES": config.get(CONF_DEVICES)
        }
    )

    # Do not mutate config_entry.data in place; an empty list means "all devices".
    device_names = config.get(CONF_DEVICES) or None

    api = JciHitachiAWSAPI(
        email=config.get(CONF_EMAIL),
        password=config.get(CONF_PASSWORD),
        device_names=device_names,
        max_retries=config.get(CONF_RETRY),
    )

    try:
        async with asyncio.timeout(LOGIN_TIMEOUT):
            await hass.async_add_executor_job(api.login)
    except AssertionError as err:
        # A configured device name is not present in the account: permanent
        # until the user fixes the configuration.
        await async_logout(hass, api)
        raise ConfigEntryError(
            f"Configured device(s) not available from the API: {err}"
        ) from err
    except RuntimeError as err:
        await async_logout(hass, api)
        if is_auth_error(err):
            raise ConfigEntryAuthFailed(f"Invalid credentials: {err}") from err
        # MQTT connect failure, device offline at boot, cloud 5xx, etc.: retry.
        raise ConfigEntryNotReady(f"Hitachi cloud not ready: {err}") from err
    except TRANSIENT_ERRORS as err:
        await async_logout(hass, api)
        raise ConfigEntryNotReady(
            f"Cannot reach Hitachi cloud (network/DNS not ready?): {err}"
        ) from err
    except Exception as err:  # noqa: BLE001
        # Never leave the entry in setup_error for an unknown reason.
        await async_logout(hass, api)
        _LOGGER.exception("Unexpected error during login")
        raise ConfigEntryNotReady(f"Unexpected error during login: {err}") from err

    hass.data[DOMAIN] = {API: api}

    _LOGGER.debug(f"Backend version: {__version__}")
    _LOGGER.debug(f"Thing info: {[thing for thing in hass.data[DOMAIN][API].things.values()]}")

    hass.data[DOMAIN][UPDATE_DATA] = Queue()
    hass.data[DOMAIN][UPDATED_DATA] = hass.data[DOMAIN][API].get_status(legacy=True)
    hass.data[DOMAIN][COORDINATOR] = build_coordinator(hass, hass.data[DOMAIN][API], config_entry=config_entry)

    # Start jcihitachi components
    _LOGGER.debug("Starting JciHitachi components.")
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Return boolean to indicate that initialization was successful.
    return True


async def async_unload_entry(hass, config_entry) -> bool:
    """Unload a config entry (enables reload from the UI and after reauth)."""

    unload_ok = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if not unload_ok:
        return False

    # Order: platforms (done above) -> coordinator -> MQTT disconnect.
    data = hass.data.pop(DOMAIN, None)
    if data:
        coordinator = data.get(COORDINATOR)
        if coordinator is not None:
            await coordinator.async_shutdown()
        api = data.get(API)
        if api is not None:
            await async_logout(hass, api)

    return True


@dataclass
class UpdateData:
    status_name : str
    device_name : str
    status_value : Optional[int] = field(default_factory=None)
    status_str_value : Optional[str] = field(default_factory=None)


class JciHitachiEntity(CoordinatorEntity):
    def __init__(self, thing, coordinator):
        super().__init__(coordinator)
        self._thing = thing

    @property
    def available(self) -> bool:
        return self._thing.available

    @property
    def device_info(self) -> dict:
        """Return device info of the entity."""
        return {
            "identifiers": {(DOMAIN, self._thing.gateway_mac_address)},
            "name": self._thing.name,
            "manufacturer": self._thing.brand,
            "model": self._thing.model,
            "sw_version": self._thing.firmware_version,
        }

    @property
    def name(self):
        """Return the thing's name."""
        return self._thing.name

    @property
    def unique_id(self):
        """Return the thing's unique id."""
        raise NotImplementedError
    
    def put_queue(self, status_name, status_value=None, status_str_value=None):
        """Put data into the queue to update status"""
        self.hass.data[DOMAIN][UPDATE_DATA].put(
            UpdateData(
                status_name=status_name,
                device_name=self._thing.name,
                status_value=status_value,
                status_str_value=status_str_value
            )
        )
    
    def update(self):
        """Update latest status."""
        api = self.hass.data[DOMAIN][API]

        while self.hass.data[DOMAIN][UPDATE_DATA].qsize() > 0:
            data = self.hass.data[DOMAIN][UPDATE_DATA].get()
            _LOGGER.debug(f"Updating data: {data}")
            result = api.set_status(**vars(data))
            if result is True:
                _LOGGER.debug(f"Data: {data} updated successfully.")
            else:
                _LOGGER.error("Failed to update data.")

        # Here we don't need to refresh status as it was refreshed by `api.set_status`.
        self.hass.data[DOMAIN][UPDATED_DATA] = api.get_status(legacy=True)
        
        _LOGGER.debug(
            f"Latest data: {[(name, value.status) for name, value in self.hass.data[DOMAIN][UPDATED_DATA].items()]}"
        )
        
        # Important: We have to reset the update scheduler to prevent old status from wrongly being loaded. 
        self.hass.loop.call_soon_threadsafe(self.coordinator.async_set_updated_data, None)
