"""Config flow for Meross LAN integration."""
from __future__ import annotations

from logging import DEBUG
from time import time
import typing

from homeassistant import config_entries
from homeassistant.const import CONF_ERROR
from homeassistant.data_entry_flow import AbortFlow, FlowHandler, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from . import MerossApi
from .const import (
    CONF_CLOUD_KEY,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_KEY,
    CONF_PASSWORD,
    CONF_PAYLOAD,
    CONF_POLLING_PERIOD,
    CONF_POLLING_PERIOD_DEFAULT,
    CONF_PROTOCOL,
    CONF_PROTOCOL_OPTIONS,
    CONF_TIMESTAMP,
    CONF_TRACE,
    CONF_TRACE_TIMEOUT,
    CONF_TRACE_TIMEOUT_DEFAULT,
    CONF_USERNAME,
    DOMAIN,
)
from .helpers import LOGGER, ApiProfile, ConfigEntriesHelper
from .merossclient import (
    MerossDeviceDescriptor,
    MerossKeyError,
    const as mc,
    get_default_arguments,
)
from .merossclient.cloudapi import CloudApiError, async_cloudapi_login
from .merossclient.httpclient import MerossHttpClient

if typing.TYPE_CHECKING:
    from .const import DeviceConfigType, ProfileConfigType
    from .meross_device import MerossDevice


# helper conf keys not persisted to config
CONF_DEVICE_TYPE = "device_type"
DESCR = "suggested_value"
ERR_BASE = "base"
ERR_CANNOT_CONNECT = "cannot_connect"
ERR_INVALID_KEY = "invalid_key"
ERR_INVALID_NULL_KEY = "invalid_nullkey"
ERR_DEVICE_ID_MISMATCH = "device_id_mismatch"
ERR_ALREADY_CONFIGURED_DEVICE = "already_configured_device"
ERR_INVALID_AUTH = "invalid_auth"
ERR_CLOUD_PROFILE_MISMATCH = "cloud_profile_mismatch"


class ConfigError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class MerossFlowHandlerMixin(FlowHandler if typing.TYPE_CHECKING else object):
    """Mixin providing commons for Config and Option flows"""

    _MENU_KEYERROR = {
        "step_id": "keyerror",
        "menu_options": ["profile", "device"],
    }

    # These values are just buffers for UI state persistance
    _username: str | None = None
    _host: str | None = None
    _key: str | None = None
    _placeholders = {
        CONF_DEVICE_TYPE: "",
        CONF_DEVICE_ID: "",
    }

    _is_keyerror: bool = False
    _httpclient: MerossHttpClient | None = None

    @callback
    def async_abort(self, *, reason: str = "already_configured"):
        return super().async_abort(reason=reason)

    def show_keyerror(self):
        self._is_keyerror = True
        return self.async_show_menu(**self._MENU_KEYERROR)

    async def async_step_profile(self, user_input=None):
        """configure a Meross cloud profile"""
        errors = {}
        _err = None

        if user_input:
            self._username = user_input[CONF_USERNAME]
            password = user_input[CONF_PASSWORD]
            try:
                credentials = await async_cloudapi_login(
                    self._username, password, async_get_clientsession(self.hass)
                )
                # this flow step is really hybrid: it could come from
                # a user flow deciding to create a profile or a user flow
                # when a device key is needed. Or, it could be an OptionFlow
                # for both of the same reasons: either a device key needed
                # or a profile configuration. In any case, we 'force' a bit
                # all of the flows logic and try to directly manage the
                # underlying ConfigEntry in a sort of a crazy generalization
                unique_id = f"profile.{credentials[mc.KEY_USERID_]}"
                title = credentials[mc.KEY_EMAIL]
                helper = ConfigEntriesHelper(self.hass)
                # abort any eventual duplicate progress flow
                profile_flow = helper.get_config_flow(unique_id)
                if (profile_flow is not None) and (
                    profile_flow["flow_id"] != self.flow_id
                ):
                    helper.config_entries.flow.async_abort(profile_flow["flow_id"])
                profile_entry = helper.get_config_entry(unique_id)
                if profile_entry is not None:
                    helper.config_entries.async_update_entry(
                        profile_entry, title=title, data=credentials
                    )
                    if profile_entry.disabled_by is not None:
                        await helper.config_entries.async_set_disabled_by(
                            profile_entry.entry_id, None
                        )

                if self._is_keyerror:
                    # this flow is managing a device
                    self._key = credentials[mc.KEY_KEY]
                    if profile_entry is None:
                        # no profile configured yet: shutdown any progress on this
                        # profile and directly create the ConfigEntry
                        await helper.config_entries.async_add(
                            config_entries.ConfigEntry(
                                version=self.VERSION,
                                domain=DOMAIN,
                                title=title,
                                data=credentials,
                                source=config_entries.SOURCE_USER,
                                unique_id=unique_id,
                            )
                        )
                    return self.async_step_device()  # type: ignore

                # this flow was managing a profile be it a user initiated one
                # or an OptionsFlow.
                return await self._async_finish_profile(title, unique_id, credentials)

            except CloudApiError as error:
                errors[CONF_ERROR] = ERR_INVALID_AUTH
                _err = str(error)
            except Exception as error:
                errors[CONF_ERROR] = ERR_CANNOT_CONNECT
                _err = str(error) or type(error).__name__

        config_schema: dict[object, object] = {
            vol.Required(CONF_USERNAME, description={DESCR: self._username}): str,
            vol.Required(CONF_PASSWORD): str,
        }
        if _err is not None:
            config_schema[vol.Optional(CONF_ERROR, description={DESCR: _err})] = str
        return self.async_show_form(
            step_id="profile",
            data_schema=vol.Schema(config_schema),
            errors=errors,
        )

    async def _async_http_discovery(
        self, host: str, key: str | None
    ) -> tuple[DeviceConfigType, MerossDeviceDescriptor]:
        # passing key=None would allow key-hack and we don't want it aymore
        if key is None:
            key = ""
        if (_httpclient := self._httpclient) is None:
            _httpclient = self._httpclient = MerossHttpClient(
                host, key, async_get_clientsession(self.hass), LOGGER  # type: ignore
            )
        else:
            _httpclient.host = host
            _httpclient.key = key

        payload = (
            await _httpclient.async_request_strict(
                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL)
            )
        )[mc.KEY_PAYLOAD]
        payload.update(
            (
                await _httpclient.async_request_strict(
                    *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ABILITY)
                )
            )[mc.KEY_PAYLOAD]
        )
        descriptor = MerossDeviceDescriptor(payload)
        return (
            {
                CONF_HOST: host,
                CONF_PAYLOAD: payload,
                CONF_KEY: key,
                CONF_DEVICE_ID: descriptor.uuid,
            },
            descriptor,
        )

    async def _async_finish_profile(self, title: str, unique_id: str, credentials):
        raise NotImplementedError()


class ConfigFlow(MerossFlowHandlerMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""

    VERSION = 1

    _MENU_USER = {
        "step_id": "user",
        "menu_options": ["profile", "device"],
    }

    _device_config: DeviceConfigType | None = None

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input=None):
        return self.async_show_menu(**self._MENU_USER)

    async def async_step_hub(self, user_input=None):
        """configure the MQTT discovery device key"""
        if user_input is None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            config_schema = {vol.Optional(CONF_KEY): str}
            return self.async_show_form(
                step_id="hub", data_schema=vol.Schema(config_schema)
            )
        return self.async_create_entry(title="MQTT Hub", data=user_input)

    async def async_step_device(self, user_input=None):
        """common device configuration"""
        errors = {}

        if user_input is None:
            if (profile := next(iter(ApiProfile.active_profiles()), None)) is not None:
                self._key = profile.key
        else:
            self._host = user_input[CONF_HOST]
            self._key = user_input.get(CONF_KEY)
            try:
                return await self._async_set_device_config(
                    *await self._async_http_discovery(self._host, self._key)
                )
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except MerossKeyError:
                return self.show_keyerror()
            except AbortFlow:
                errors[ERR_BASE] = ERR_ALREADY_CONFIGURED_DEVICE
            except Exception as error:
                LOGGER.warning(
                    "Error (%s) configuring meross device (host:%s)",
                    str(error),
                    self._host,
                )
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, description={DESCR: self._host}): str,
                    vol.Optional(CONF_KEY, description={DESCR: self._key}): str,
                }
            ),
            errors=errors,
            description_placeholders=self._placeholders,
        )

    async def async_step_integration_discovery(self, discovery_info: DeviceConfigType):
        """
        this is actually the entry point for devices discovered through our MQTTConnection(s)
        or to trigger a cloud profile configuration when migrating older config entries
        """
        if mc.KEY_USERID_ in discovery_info:
            return await self.async_step_profile()

        return await self._async_set_device_config(
            discovery_info, MerossDeviceDescriptor(discovery_info[CONF_PAYLOAD])
        )

    async def async_step_dhcp(self, discovery_info):
        """Handle a flow initialized by DHCP discovery."""
        if LOGGER.isEnabledFor(DEBUG):
            LOGGER.debug("received dhcp discovery: %s", str(discovery_info))
        host = discovery_info.ip
        macaddress = discovery_info.macaddress.replace(":", "").lower()
        # check if the device is already registered
        try:
            entries = self.hass.config_entries
            for entry in entries.async_entries(DOMAIN):
                descriptor = MerossDeviceDescriptor(entry.data.get(CONF_PAYLOAD))
                if descriptor.macAddress.replace(":", "").lower() != macaddress:
                    continue
                if entry.data.get(CONF_HOST) != host:
                    data = dict(entry.data)
                    data[CONF_HOST] = host
                    data[CONF_TIMESTAMP] = time()  # force ConfigEntry update..
                    entries.async_update_entry(entry, data=data)
                    LOGGER.info(
                        "DHCP updated device ip address (%s) for device %s",
                        host,
                        descriptor.uuid,
                    )
                return self.async_abort()
        except Exception as error:
            LOGGER.warning("DHCP update internal error: %s", str(error))
        # we'll update the unique_id for the flow when we'll have the device_id
        # Here this is needed in case we cannot correctly identify the device
        # via our api and the dhcp integration keeps pushing us discoveries for
        # the same device
        # update 2022-12-19: adding DOMAIN prefix since macaddress alone might be set by other
        # integrations and that would conflict with our unique_id likely raising issues
        # on DHCP discovery not working in some configurations
        await self.async_set_unique_id(DOMAIN + macaddress, raise_on_progress=True)

        try:
            # try device identification so the user/UI has a good context to start with
            _device_config = _descriptor = None
            for profile in ApiProfile.active_profiles():
                try:
                    _device_config, _descriptor = await self._async_http_discovery(
                        host, profile.key
                    )
                    # deeply check the device is really bounded to the profile
                    # since the key might luckily be good even tho the profile not
                    if _descriptor.userId == profile[mc.KEY_USERID_]:
                        self._key = profile.key
                        break
                except:
                    pass
                _device_config = _descriptor = None

            if (_device_config is None) and ((key := ApiProfile.api.key) is not None):
                try:
                    _device_config, _descriptor = await self._async_http_discovery(
                        host, key
                    )
                    self._key = key
                except:
                    pass

            if _device_config is not None:
                return await self._async_set_device_config(_device_config, _descriptor)  # type: ignore

        except Exception as exception:
            if LOGGER.isEnabledFor(DEBUG):
                LOGGER.debug(
                    "%s(%s) identifying meross device (host:%s)",
                    exception.__class__.__name__,
                    str(exception),
                    host,
                )
            if isinstance(exception, AbortFlow):
                # we might have 'correctly' identified an already configured entry
                return self.async_abort()
            # forgive and continue if we cant discover the device...let the user work it out

        self._host = host
        return await self.async_step_device()

    async def async_step_mqtt(self, discovery_info):
        """manage the MQTT discovery flow"""
        await self.async_set_unique_id(DOMAIN)
        # this entry should only ever called once after startup
        # when HA thinks we're interested in discovery.
        # If our MerossApi is already running it will manage the discovery itself
        # so this flow is only useful when MerossLan has no configuration yet
        # and we leverage the default mqtt discovery to setup our manager
        api = MerossApi.get(self.hass)
        if api.mqtt_is_subscribed():
            return self.async_abort()
        # try setup the mqtt subscription
        # this call might not register because of errors or because of an overlapping
        # request from 'async_setup_entry' (we're preventing overlapped calls to MQTT
        # subscription)
        await api.async_mqtt_register()
        if api.mqtt_is_subscribed():
            # ok, now pass along the discovering mqtt message so our MerossApi state machine
            # gets to work on this
            await api.async_mqtt_message(discovery_info)
        # just in case, setup the MQTT Hub entry to enable the (default) device key configuration
        # if the entry hub is already configured this will disable the discovery
        # subscription (by returning 'already_configured') stopping any subsequent async_step_mqtt message:
        # our MerossApi should already be in place
        return await self.async_step_hub()

    async def async_step_finalize(self, user_input=None):
        return self.async_create_entry(
            title=self._title,
            data=self._device_config,  # type: ignore
        )

    async def _async_set_device_config(
        self, data: DeviceConfigType, descriptor: MerossDeviceDescriptor
    ):
        self._device_config = data
        self._descriptor = descriptor
        device_id = descriptor.uuid
        if (
            ((profile_id := descriptor.userId) in ApiProfile.profiles)
            and ((profile := ApiProfile.profiles.get(profile_id)) is not None)
            and ((device_info := profile.get_device_info(device_id)) is not None)
        ):
            devname = device_info.get(mc.KEY_DEVNAME, device_id)
        else:
            devname = device_id
        self._title = f"{descriptor.type} - {devname}"
        self.context["title_placeholders"] = {"name": self._title}
        self._placeholders = {
            CONF_DEVICE_TYPE: descriptor.productnametype,
            CONF_DEVICE_ID: device_id,
        }
        if await self.async_set_unique_id(device_id) is not None:
            raise AbortFlow("already_configured")

        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders,
        )

    async def _async_finish_profile(self, title: str, unique_id: str, credentials):
        if await self.async_set_unique_id(unique_id) is not None:
            return self.async_abort()
        return self.async_create_entry(title=title, data=credentials)


class OptionsFlowHandler(MerossFlowHandlerMixin, config_entries.OptionsFlow):
    """
    Manage device options configuration
    """

    _trace: bool  # this is the UI value (yes or no) CONF_TRACE carries endtime

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        unique_id = self._config_entry.unique_id
        if unique_id == DOMAIN:
            return await self.async_step_hub()

        unique_id = unique_id.split(".")  # type: ignore
        if unique_id[0] == "profile":
            data: ProfileConfigType = self._config_entry.data  # type: ignore
            self._username = data.get(mc.KEY_EMAIL)
            return await self.async_step_profile()

        data: DeviceConfigType = self._config_entry.data  # type: ignore
        self._device_id = unique_id[0]
        assert self._device_id == data.get(CONF_DEVICE_ID)
        self._host = data.get(CONF_HOST)  # null for devices discovered over mqtt
        self._key = data.get(CONF_KEY)
        self._protocol = data.get(CONF_PROTOCOL)
        self._polling_period = data.get(CONF_POLLING_PERIOD)
        self._trace = (data.get(CONF_TRACE) or 0) > time()
        self._trace_timeout = data.get(CONF_TRACE_TIMEOUT)
        self._placeholders = {
            CONF_DEVICE_ID: self._device_id,
            CONF_HOST: self._host or "MQTT",
        }
        return await self.async_step_device()

    async def async_step_hub(self, user_input=None):
        if user_input is not None:
            data = dict(self._config_entry.data)
            data[CONF_KEY] = user_input.get(CONF_KEY)
            self.hass.config_entries.async_update_entry(self._config_entry, data=data)
            return self.async_create_entry(data=None)  # type: ignore

        config_schema = {
            vol.Optional(
                CONF_KEY, description={DESCR: self._config_entry.data.get(CONF_KEY)}
            ): str
        }
        return self.async_show_form(
            step_id="hub", data_schema=vol.Schema(config_schema)
        )

    async def async_step_device(self, user_input: DeviceConfigType | None = None):
        """
        general (common) device configuration allowing key set and
        general parameters to be entered/modified
        """
        errors = {}
        device: MerossDevice = ApiProfile.devices[self._device_id]  # type: ignore
        if user_input is not None:
            self._host = user_input.get(CONF_HOST)
            self._key = user_input.get(CONF_KEY)
            self._protocol = user_input.get(CONF_PROTOCOL)
            self._polling_period = user_input.get(CONF_POLLING_PERIOD)
            self._trace = user_input.get(CONF_TRACE)  # type: ignore
            self._trace_timeout = user_input.get(
                CONF_TRACE_TIMEOUT, CONF_TRACE_TIMEOUT_DEFAULT
            )
            try:
                if self._host is None:
                    # this device has been discovered by mqtt and has no http
                    # reachability in config..we still lack a lot of stuff
                    # to fix this but this should be a less common scenario since
                    # most of the users should have added devices discovered on http
                    # which would be treated in the other branch
                    # TODO: implement mqtt connection check and validation
                    _descriptor = device.descriptor
                else:
                    _device_config, _descriptor = await self._async_http_discovery(
                        self._host, self._key
                    )
                    if self._device_id != _descriptor.uuid:
                        raise ConfigError(ERR_DEVICE_ID_MISMATCH)
                data = dict(self._config_entry.data)
                if self._host is not None:
                    data[CONF_HOST] = self._host
                    data[CONF_PAYLOAD] = _device_config[CONF_PAYLOAD]  # type: ignore

                data[CONF_KEY] = self._key
                data[CONF_PROTOCOL] = self._protocol
                data[CONF_POLLING_PERIOD] = self._polling_period
                if self._trace:
                    data[CONF_TRACE] = time() + (
                        self._trace_timeout or CONF_TRACE_TIMEOUT_DEFAULT
                    )
                else:
                    data.pop(CONF_TRACE, None)
                data[CONF_TRACE_TIMEOUT] = self._trace_timeout
                try:
                    device.entry_option_update(user_input)
                except:
                    pass  # forgive any error

                if CONF_CLOUD_KEY in data:
                    # cloud_key functionality has been superseeded by
                    # meross cloud profiles and we could just remove it.
                    # Actually, we leave it in place as a way to 'force/trigger'
                    # the user to properly configure a meross cloud profile.
                    # In fact it is checked when loading the device config entry
                    # to see if a (profile) flow need to be started
                    if _descriptor.userId in ApiProfile.profiles:
                        data.pop(CONF_CLOUD_KEY)
                # we're not following HA 'etiquette' and we're just updating the
                # config_entry data with this dirty trick
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=data
                )
                # return None in data so the async_update_entry is not called for the
                # options to be updated
                return self.async_create_entry(data=None)  # type: ignore

            except MerossKeyError:
                return self.show_keyerror()
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except Exception:
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        config_schema = {}
        if self._host is not None:
            config_schema[
                vol.Required(CONF_HOST, description={DESCR: self._host})
            ] = str
        config_schema[vol.Optional(CONF_KEY, description={DESCR: self._key})] = str
        config_schema[
            vol.Optional(CONF_PROTOCOL, description={DESCR: self._protocol})
        ] = vol.In(CONF_PROTOCOL_OPTIONS.keys())
        config_schema[
            vol.Optional(
                CONF_POLLING_PERIOD,
                default=CONF_POLLING_PERIOD_DEFAULT,  # type: ignore
                description={DESCR: self._polling_period},
            )
        ] = cv.positive_int
        # setup device specific config right before last option
        self._placeholders[CONF_DEVICE_TYPE] = device.descriptor.productnametype
        try:
            device.entry_option_setup(config_schema)
        except:
            pass  # forgive any error

        config_schema[
            vol.Optional(
                CONF_TRACE,
                # CONF_TRACE contains the trace 'end' time epoch if set
                description={DESCR: self._trace},
            )
        ] = bool
        config_schema[
            vol.Optional(
                CONF_TRACE_TIMEOUT,
                default=CONF_TRACE_TIMEOUT_DEFAULT,  # type: ignore
                description={DESCR: self._trace_timeout},
            )
        ] = cv.positive_int

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders=self._placeholders,
            errors=errors,
        )

    async def _async_finish_profile(self, title: str, unique_id: str, credentials):
        return self.async_create_entry(data=None)  # type: ignore
