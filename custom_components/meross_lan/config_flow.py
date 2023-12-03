"""Config flow for Meross LAN integration."""
from __future__ import annotations

from logging import DEBUG
from time import time
import typing

from homeassistant import config_entries
from homeassistant.const import CONF_ERROR
from homeassistant.data_entry_flow import AbortFlow, FlowHandler, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from . import MerossApi, const as mlc
from .helpers import LOGGER, ApiProfile, ConfigEntriesHelper
from .merossclient import (
    MerossDeviceDescriptor,
    MerossKeyError,
    const as mc,
    get_default_arguments,
)
from .merossclient.cloudapi import (
    CloudApiError,
    async_cloudapi_login,
    async_cloudapi_logout_safe,
)
from .merossclient.httpclient import MerossHttpClient


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

    # this is set for an OptionsFlow
    _profile_entry: config_entries.ConfigEntry | None = None

    # These values are just buffers for UI state persistance
    hub_config: mlc.HubConfigType
    device_config: mlc.DeviceConfigType
    profile_config: mlc.ProfileConfigType
    device_descriptor: MerossDeviceDescriptor

    _placeholders = {
        CONF_DEVICE_TYPE: "",
        mlc.CONF_DEVICE_ID: "",
        mlc.CONF_HOST: "",
    }

    _is_keyerror: bool = False
    _httpclient: MerossHttpClient | None = None

    @callback
    def async_abort(self, *, reason: str = "already_configured"):
        return super().async_abort(reason=reason)

    async def async_step_profile(self, user_input=None):
        """configure a Meross cloud profile"""
        errors = {}
        _err = profile = None
        profile_config = self.profile_config

        if user_input:
            # profile_config has both user set keys (updated through user_input)
            # and MerossCloudCredentials keys (updated when logging into Meross http api)
            profile_config.update(user_input)
            try:
                # this flow step is really hybrid: it could come from
                # a user flow deciding to create a profile or a user flow
                # when a device key is needed. Or, it could be an OptionFlow
                # for both of the same reasons: either a device key needed
                # or a profile configuration. In any case, we 'force' a bit
                # all of the flows logic and try to directly manage the
                # underlying ConfigEntry in a sort of a crazy generalization
                if mlc.CONF_PASSWORD in user_input:
                    credentials = await async_cloudapi_login(
                        profile_config[mlc.CONF_EMAIL],
                        user_input[mlc.CONF_PASSWORD],
                        async_get_clientsession(self.hass),
                    )
                    if (
                        mc.KEY_USERID_ in profile_config
                        and credentials[mc.KEY_USERID_]
                        != profile_config[mc.KEY_USERID_]
                    ):
                        await async_cloudapi_logout_safe(
                            credentials[mc.KEY_TOKEN],
                            async_get_clientsession(self.hass),
                        )
                        raise ConfigError(ERR_CLOUD_PROFILE_MISMATCH)
                    profile_config.update(credentials)  # type: ignore
                    if not user_input.get(mlc.CONF_SAVE_PASSWORD):
                        profile_config.pop(mlc.CONF_PASSWORD, None)

                if self._profile_entry:
                    # we were managing a profile OptionsFlow: fast save
                    self.hass.config_entries.async_update_entry(
                        self._profile_entry, data=profile_config
                    )
                    return self.async_create_entry(data=None)  # type: ignore

                # abort any eventual duplicate progress flow
                # also, even if the user was creating a new profile,
                # updates any eventually existing one...
                # we will eventually abort this flow later
                unique_id = f"profile.{profile_config[mc.KEY_USERID_]}"
                helper = ConfigEntriesHelper(self.hass)
                profile_flow = helper.get_config_flow(unique_id)
                if profile_flow and (profile_flow["flow_id"] != self.flow_id):
                    helper.config_entries.flow.async_abort(profile_flow["flow_id"])
                profile_entry = helper.get_config_entry(unique_id)
                if profile_entry:
                    helper.config_entries.async_update_entry(
                        profile_entry,
                        title=profile_config[mc.KEY_EMAIL],
                        data=profile_config,
                    )
                    if not self._is_keyerror:
                        # this flow was creating a profile but it's entry is
                        # already in place (and updated)
                        return self.async_abort()
                else:
                    if self._is_keyerror:
                        # this flow is managing a device but since the profile
                        # entry is new, we'll directly setup that
                        await helper.config_entries.async_add(
                            config_entries.ConfigEntry(
                                version=self.VERSION,
                                domain=mlc.DOMAIN,
                                title=profile_config[mc.KEY_EMAIL],
                                data=profile_config,
                                source=config_entries.SOURCE_USER,
                                unique_id=unique_id,
                            )
                        )
                    else:
                        # this ConfigFlow was creating(user) a profile and looks like
                        # no entry exists
                        if await self.async_set_unique_id(unique_id, raise_on_progress=False):  # type: ignore
                            return self.async_abort()
                        return self.async_create_entry(
                            title=profile_config[mc.KEY_EMAIL], data=profile_config
                        )

                # this flow is managing a device: assert self._is_keyerror
                self.device_config[mlc.CONF_KEY] = profile_config[mc.KEY_KEY]
                return await self.async_step_device()

            except CloudApiError as error:
                errors[CONF_ERROR] = ERR_INVALID_AUTH
                _err = str(error)
            except ConfigError as error:
                errors[CONF_ERROR] = ERR_INVALID_AUTH
                _err = error.reason
            except Exception as error:
                errors[CONF_ERROR] = ERR_CANNOT_CONNECT
                _err = str(error) or type(error).__name__

        config_schema = {}
        if _err:
            config_schema[vol.Optional(CONF_ERROR, description={DESCR: _err})] = str
        if self._profile_entry:
            profile = ApiProfile.profiles.get(profile_config[mc.KEY_USERID_])
            require_login = not (profile and profile.token)
        else:
            # this is not a profile OptionsFlow so we'd need to login for sure
            # with full credentials
            config_schema[
                vol.Required(
                    mlc.CONF_EMAIL,
                    description={DESCR: profile_config.get(mlc.CONF_EMAIL)},
                )
            ] = str
            require_login = True
        if require_login:
            # token expired or not a profile OptionFlow: we'd need to login again
            config_schema[
                vol.Required(
                    mlc.CONF_PASSWORD,
                    description={DESCR: profile_config.get(mlc.CONF_PASSWORD)},
                )
            ] = str
            config_schema[
                vol.Optional(
                    mlc.CONF_SAVE_PASSWORD,
                    description={DESCR: profile_config.get(mlc.CONF_SAVE_PASSWORD)},
                )
            ] = bool
        config_schema[
            vol.Optional(
                mlc.CONF_ALLOW_MQTT_PUBLISH,
                description={DESCR: profile_config.get(mlc.CONF_ALLOW_MQTT_PUBLISH)},
            )
        ] = bool
        config_schema[
            vol.Optional(
                mlc.CONF_CHECK_FIRMWARE_UPDATES,
                description={
                    DESCR: profile_config.get(mlc.CONF_CHECK_FIRMWARE_UPDATES)
                },
            )
        ] = bool
        config_schema[
            vol.Optional(
                mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES,
                description={
                    DESCR: profile_config.get(mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES)
                },
            )
        ] = bool

        return self.async_show_form(
            step_id="profile",
            data_schema=vol.Schema(config_schema),
            errors=errors,
        )

    async def async_step_device(self, user_input=None):
        raise NotImplementedError()

    async def async_step_keyerror(self, user_input=None):
        self._is_keyerror = True
        self.profile_config = {}  # type: ignore[assignment]
        return self.async_show_menu(
            step_id="keyerror", menu_options=["profile", "device"]
        )

    async def _async_http_discovery(
        self, host: str, key: str | None
    ) -> tuple[mlc.DeviceConfigType, MerossDeviceDescriptor]:
        # passing key=None would allow key-hack and we don't want it aymore
        if key is None:
            key = ""
        if _httpclient := self._httpclient:
            _httpclient.host = host
            _httpclient.key = key
        else:
            self._httpclient = _httpclient = MerossHttpClient(
                host, key, async_get_clientsession(self.hass), LOGGER  # type: ignore
            )

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
                mlc.CONF_HOST: host,
                mlc.CONF_PAYLOAD: payload,
                mlc.CONF_KEY: key,
                mlc.CONF_DEVICE_ID: descriptor.uuid,
            },
            descriptor,
        )

    async def _async_mqtt_discovery(
        self, device_id: str, key: str | None, profile_id: str | None
    ) -> tuple[mlc.DeviceConfigType, MerossDeviceDescriptor]:
        # passing key=None would allow key-hack and we don't want it aymore
        if key is None:
            key = ""

        mqttconnections = []
        # TODO: we should better detect if the profile is a Meross one
        # and eventually raise a better exception stating if it's available
        # or not (disabled maybe)
        if profile_id and (profile := MerossApi.profiles.get(profile_id)):
            mqttconnections = profile.get_or_create_mqttconnections(device_id)
        else:
            mqttconnections = [MerossApi.get(self.hass).mqtt_connection]

        payload = None
        for mqttconnection in mqttconnections:
            response = await mqttconnection.async_mqtt_publish(
                device_id,
                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ALL),
                key,
            )
            if not isinstance(response, dict):
                continue  # try next connection if any
            payload = response[mc.KEY_PAYLOAD]
            response = await mqttconnection.async_mqtt_publish(
                device_id,
                *get_default_arguments(mc.NS_APPLIANCE_SYSTEM_ABILITY),
                key,
            )
            if not isinstance(response, dict):
                payload = None
                continue  # try next connection if any
            payload.update(response[mc.KEY_PAYLOAD])
            descriptor = MerossDeviceDescriptor(payload)
            return (
                {
                    mlc.CONF_PAYLOAD: payload,
                    mlc.CONF_KEY: key,
                    mlc.CONF_DEVICE_ID: descriptor.uuid,
                },
                descriptor,
            )

        raise Exception(
            "No MQTT response: either no available broker or invalid device id"
        )


class ConfigFlow(MerossFlowHandlerMixin, config_entries.ConfigFlow, domain=mlc.DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""

    VERSION = 1

    _MENU_USER = {
        "step_id": "user",
        "menu_options": ["profile", "device"],
    }

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        self.device_config = {}  # type: ignore[assignment]
        self.profile_config = {}  # type: ignore[assignment]
        return self.async_show_menu(**self._MENU_USER)

    async def async_step_hub(self, user_input=None):
        """configure the MQTT discovery device key"""
        if user_input is None:
            await self.async_set_unique_id(mlc.DOMAIN)
            self._abort_if_unique_id_configured()
            config_schema = {vol.Optional(mlc.CONF_KEY): str}
            return self.async_show_form(
                step_id="hub", data_schema=vol.Schema(config_schema)
            )
        return self.async_create_entry(title="MQTT Hub", data=user_input)

    async def async_step_device(self, user_input=None):
        """common device configuration"""
        errors = {}
        device_config = self.device_config

        if user_input is None:
            if profile := next(iter(ApiProfile.active_profiles()), None):
                device_config[mlc.CONF_KEY] = profile.key
        else:
            device_config.update(user_input)
            try:
                return await self._async_set_device_config(
                    *await self._async_http_discovery(
                        user_input[mlc.CONF_HOST], user_input.get(mlc.CONF_KEY)
                    )
                )
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except MerossKeyError:
                return await self.async_step_keyerror()
            except AbortFlow:
                errors[ERR_BASE] = ERR_ALREADY_CONFIGURED_DEVICE
            except Exception as error:
                LOGGER.warning(
                    "Error (%s) configuring meross device (host:%s)",
                    str(error),
                    user_input[mlc.CONF_HOST],
                )
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        mlc.CONF_HOST,
                        description={DESCR: device_config.get(mlc.CONF_HOST)},
                    ): str,
                    vol.Optional(
                        mlc.CONF_KEY,
                        description={DESCR: device_config.get(mlc.CONF_KEY)},
                    ): str,
                }
            ),
            errors=errors,
            description_placeholders=self._placeholders,
        )

    async def async_step_integration_discovery(
        self, discovery_info: mlc.DeviceConfigType | mlc.ProfileConfigType
    ):
        """
        this is actually the entry point for devices discovered through our MQTTConnection(s)
        or to trigger a cloud profile configuration when migrating older config entries
        """
        if mc.KEY_USERID_ in discovery_info:
            self.profile_config = discovery_info  # type: ignore
            return await self.async_step_profile()

        return await self._async_set_device_config(
            discovery_info, MerossDeviceDescriptor(discovery_info[mlc.CONF_PAYLOAD])
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
            for entry in entries.async_entries(mlc.DOMAIN):
                entry_data = entry.data
                entry_descriptor = MerossDeviceDescriptor(
                    entry_data.get(mlc.CONF_PAYLOAD)
                )
                if entry_descriptor.macAddress.replace(":", "").lower() != macaddress:
                    continue
                if entry_data.get(mlc.CONF_HOST) != host:
                    # before updating, check the host ip is 'really' valid
                    try:
                        _device_config, _descriptor = await self._async_http_discovery(
                            host, entry_data.get(mlc.CONF_KEY)
                        )
                        if (
                            _device_config[mlc.CONF_DEVICE_ID]
                            == entry_data[mlc.CONF_DEVICE_ID]
                        ):
                            data = dict(entry_data)
                            data.update(_device_config)
                            data[
                                mlc.CONF_TIMESTAMP
                            ] = time()  # force ConfigEntry update..
                            entries.async_update_entry(entry, data=data)
                            LOGGER.info(
                                "DHCP updated {ip=%s, mac=%s} for device %s",
                                host,
                                discovery_info.macaddress,
                                entry_descriptor.uuid,
                            )
                        else:
                            LOGGER.error(
                                "received a DHCP update {ip=%s, mac=%s} but the new device {uuid=%s} doesn't match the configured one {uuid=%s}",
                                host,
                                discovery_info.macaddress,
                                _descriptor.uuid,
                                entry_descriptor.uuid,
                            )

                    except Exception as error:
                        LOGGER.warning(
                            "DHCP update error %s trying to identify device {uuid=%s} at {ip=%s, mac=%s}",
                            str(error),
                            entry_descriptor.uuid,
                            host,
                            discovery_info.macaddress,
                        )

                return self.async_abort()
        except Exception as error:
            LOGGER.warning("DHCP update internal error: %s", str(error))
        # we'll update the unique_id for the flow when we'll have the device_id
        # Here this is needed in case we cannot correctly identify the device
        # via our api and the dhcp integration keeps pushing us discoveries for
        # the same device
        # update 2022-12-19: adding mlc.DOMAIN prefix since macaddress alone might be set by other
        # integrations and that would conflict with our unique_id likely raising issues
        # on DHCP discovery not working in some configurations
        await self.async_set_unique_id(mlc.DOMAIN + macaddress, raise_on_progress=True)

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
                    if _descriptor.userId == profile.id:
                        break
                except Exception:
                    pass
                _device_config = _descriptor = None

            if (not _device_config) and (key := ApiProfile.api.key):
                try:
                    _device_config, _descriptor = await self._async_http_discovery(
                        host, key
                    )
                except Exception:
                    pass

            if _device_config:
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

        self.device_config = {  # type: ignore
            mlc.CONF_HOST: host,
        }
        return await self.async_step_device()

    async def async_step_mqtt(self, discovery_info):
        """manage the MQTT discovery flow"""
        # this entry should only ever called once after startup
        # when HA thinks we're interested in discovery.
        # If our MerossApi is already running it will manage the discovery itself
        # so this flow is only useful when MerossLan has no configuration yet
        # and we leverage the default mqtt discovery to setup our manager
        mqtt_connection = MerossApi.get(self.hass).mqtt_connection
        if mqtt_connection.mqtt_is_subscribed:
            return self.async_abort()
        # try setup the mqtt subscription
        # this call might not register because of errors or because of an overlapping
        # request from 'async_setup_entry' (we're preventing overlapped calls to MQTT
        # subscription)
        if await mqtt_connection.async_mqtt_subscribe():
            # ok, now pass along the discovering mqtt message so our MerossApi state machine
            # gets to work on this
            await mqtt_connection.async_mqtt_message(discovery_info)
        # just in case, setup the MQTT Hub entry to enable the (default) device key configuration
        # if the entry hub is already configured this will disable the discovery
        # subscription (by returning 'already_configured') stopping any subsequent async_step_mqtt message:
        # our MerossApi should already be in place
        return await self.async_step_hub()

    async def async_step_finalize(self, user_input=None):
        return self.async_create_entry(
            title=self._title,
            data=self.device_config,
        )

    async def _async_set_device_config(
        self, device_config: mlc.DeviceConfigType, descriptor: MerossDeviceDescriptor
    ):
        self.device_config = device_config
        self._descriptor = descriptor
        device_id = descriptor.uuid
        if (
            ((profile_id := descriptor.userId) in ApiProfile.profiles)
            and (profile := ApiProfile.profiles.get(profile_id))
            and (device_info := profile.get_device_info(device_id))
        ):
            devname = device_info.get(mc.KEY_DEVNAME, device_id)
        else:
            devname = device_id
        self._title = f"{descriptor.type} - {devname}"
        self.context["title_placeholders"] = {"name": self._title}
        self._placeholders = {
            CONF_DEVICE_TYPE: descriptor.productnametype,
            mlc.CONF_DEVICE_ID: device_id,
        }
        if await self.async_set_unique_id(device_id):
            raise AbortFlow("already_configured")

        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self._placeholders,
        )


class OptionsFlow(MerossFlowHandlerMixin, config_entries.OptionsFlow):
    """
    Manage device options configuration
    """

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        unique_id = self._config_entry.unique_id
        if unique_id == mlc.DOMAIN:
            self.hub_config = dict(self._config_entry.data)  # type: ignore
            return await self.async_step_hub()

        unique_id = unique_id.split(".")  # type: ignore
        if unique_id[0] == "profile":
            self._profile_entry = self._config_entry
            self.profile_config = dict(self._config_entry.data)  # type: ignore
            return await self.async_step_profile()

        self.device_config = dict(self._config_entry.data)  # type: ignore
        self._device_id = unique_id[0]
        assert self._device_id == self.device_config.get(mlc.CONF_DEVICE_ID)
        device = ApiProfile.devices[self._device_id]
        # if config not loaded the device is None
        self.device_descriptor = (
            device.descriptor
            if device
            else MerossDeviceDescriptor(self.device_config.get(mlc.CONF_PAYLOAD))
        )
        self._placeholders = {
            mlc.CONF_DEVICE_ID: self._device_id,
            CONF_DEVICE_TYPE: self.device_descriptor.productnametype,
        }
        return await self.async_step_device()

    async def async_step_hub(self, user_input=None):
        hub_config = self.hub_config
        if user_input is not None:
            hub_config[mlc.CONF_KEY] = user_input.get(mlc.CONF_KEY)
            self.hass.config_entries.async_update_entry(
                self._config_entry, data=hub_config
            )
            return self.async_create_entry(data=None)  # type: ignore

        config_schema = {
            vol.Optional(
                mlc.CONF_KEY, description={DESCR: hub_config.get(mlc.CONF_KEY)}
            ): str
        }
        return self.async_show_form(
            step_id="hub", data_schema=vol.Schema(config_schema)
        )

    async def async_step_device(self, user_input: mlc.DeviceConfigType | None = None):
        """
        general (common) device configuration allowing key set and
        general parameters to be entered/modified
        """
        errors = {}
        device = ApiProfile.devices[self._device_id]
        device_config = self.device_config
        if user_input is not None:
            device_config.update(user_input)
            try:
                inner_exception = None
                device_config_update = None
                descriptor_update = None
                _host = user_input.get(mlc.CONF_HOST)
                _key = user_input.get(mlc.CONF_KEY)
                _conf_protocol = mlc.CONF_PROTOCOL_OPTIONS.get(
                    user_input.get(mlc.CONF_PROTOCOL), mlc.CONF_PROTOCOL_AUTO
                )
                if _conf_protocol is not mlc.CONF_PROTOCOL_HTTP:
                    try:
                        (
                            device_config_update,
                            descriptor_update,
                        ) = await self._async_mqtt_discovery(
                            self._device_id, _key, self.device_descriptor.userId
                        )
                    except Exception as e:
                        inner_exception = e
                if _conf_protocol is not mlc.CONF_PROTOCOL_MQTT:
                    if _host:
                        try:
                            (
                                device_config_update,
                                descriptor_update,
                            ) = await self._async_http_discovery(_host, _key)
                        except Exception as e:
                            inner_exception = e

                if not device_config_update or not descriptor_update:
                    raise inner_exception or ConfigError(ERR_CANNOT_CONNECT)
                if self._device_id != device_config_update[mlc.CONF_DEVICE_ID]:
                    raise ConfigError(ERR_DEVICE_ID_MISMATCH)
                if _host:
                    device_config[mlc.CONF_HOST] = _host
                else:
                    device_config.pop(mlc.CONF_HOST, None)
                device_config[mlc.CONF_PAYLOAD] = device_config_update[mlc.CONF_PAYLOAD]
                if device_config.get(mlc.CONF_TRACE):
                    device_config[mlc.CONF_TRACE] = time() + (
                        device_config.get(mlc.CONF_TRACE_TIMEOUT)
                        or mlc.CONF_TRACE_TIMEOUT_DEFAULT
                    )
                else:
                    device_config.pop(mlc.CONF_TRACE, None)
                if mlc.CONF_CLOUD_KEY in device_config:
                    # cloud_key functionality has been superseeded by
                    # meross cloud profiles and we could just remove it.
                    # Actually, we leave it in place as a way to 'force/trigger'
                    # the user to properly configure a meross cloud profile.
                    # In fact it is checked when loading the device config entry
                    # to see if a (profile) flow need to be started
                    if descriptor_update.userId in ApiProfile.profiles:
                        device_config.pop(mlc.CONF_CLOUD_KEY)
                if device:
                    try:
                        device.entry_option_update(user_input)
                    except Exception:
                        pass  # forgive any error
                # we're not following HA 'etiquette' and we're just updating the
                # config_entry data with this dirty trick
                self.hass.config_entries.async_update_entry(
                    self._config_entry, data=device_config
                )
                if (
                    self._config_entry.state
                    == config_entries.ConfigEntryState.SETUP_ERROR
                ):
                    try:  # to fix the device registry in case it was corrupted by #341
                        device_registry = dr.async_get(self.hass)
                        device_identifiers = {(str(mlc.DOMAIN), self._device_id)}
                        device_entry = device_registry.async_get_device(
                            identifiers=device_identifiers
                        )
                        if device_entry and (
                            len(device_entry.connections) > 1
                            or len(device_entry.config_entries) > 1
                        ):
                            _area_id = device_entry.area_id
                            _name_by_user = device_entry.name_by_user
                            device_registry.async_remove_device(device_entry.id)
                            device_registry.async_get_or_create(
                                config_entry_id=self._config_entry.entry_id,
                                suggested_area=_area_id,
                                name=descriptor_update.productname,
                                model=descriptor_update.productmodel,
                                hw_version=descriptor_update.hardwareVersion,
                                sw_version=descriptor_update.firmwareVersion,
                                manufacturer=mc.MANUFACTURER,
                                connections={
                                    (
                                        dr.CONNECTION_NETWORK_MAC,
                                        descriptor_update.macAddress,
                                    )
                                },
                                identifiers=device_identifiers,
                            )
                            LOGGER.warning(
                                "Device registry entry for %s (uuid:%s) was updated in order to fix it. The friendly name ('%s') has been lost and needs to be manually re-entered",
                                descriptor_update.productmodel,
                                self._device_id,
                                _name_by_user,
                            )

                    except Exception as error:
                        LOGGER.warning(
                            "error (%s) while trying to repair device registry for %s (uuid:%s)",
                            str(error),
                            descriptor_update.productmodel,
                            self._device_id
                        )
                        pass

                    await self.hass.config_entries.async_reload(
                        self._config_entry.entry_id
                    )
                # return None in data so the async_update_entry is not called for the
                # options to be updated
                return self.async_create_entry(data=None)  # type: ignore

            except MerossKeyError:
                return await self.async_step_keyerror()
            except ConfigError as error:
                errors[ERR_BASE] = error.reason
            except Exception:
                errors[ERR_BASE] = ERR_CANNOT_CONNECT

        config_schema = {}
        _host = device_config.get(mlc.CONF_HOST)
        config_schema[vol.Optional(mlc.CONF_HOST, description={DESCR: _host})] = str
        self._placeholders[mlc.CONF_HOST] = _host or "MQTT"
        config_schema[
            vol.Optional(
                mlc.CONF_KEY, description={DESCR: device_config.get(mlc.CONF_KEY)}
            )
        ] = str
        config_schema[
            vol.Optional(
                mlc.CONF_PROTOCOL,
                description={DESCR: device_config.get(mlc.CONF_PROTOCOL)},
            )
        ] = vol.In(mlc.CONF_PROTOCOL_OPTIONS.keys())
        config_schema[
            vol.Optional(
                mlc.CONF_POLLING_PERIOD,
                default=mlc.CONF_POLLING_PERIOD_DEFAULT,  # type: ignore
                description={DESCR: device_config.get(mlc.CONF_POLLING_PERIOD)},
            )
        ] = cv.positive_int
        # setup device specific config right before last option
        if device:
            try:
                device.entry_option_setup(config_schema)
            except Exception:
                pass  # forgive any error

        config_schema[
            vol.Optional(
                mlc.CONF_TRACE,
                default=False,  # type: ignore
                # this is the UI value (yes or no) mlc.CONF_TRACE carries
                # trace endtime if racing is active
                description={DESCR: (device_config.get(mlc.CONF_TRACE) or 0) > time()},
            )
        ] = bool
        config_schema[
            vol.Optional(
                mlc.CONF_TRACE_TIMEOUT,
                default=mlc.CONF_TRACE_TIMEOUT_DEFAULT,  # type: ignore
                description={DESCR: device_config.get(mlc.CONF_TRACE_TIMEOUT)},
            )
        ] = cv.positive_int

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(config_schema),
            description_placeholders=self._placeholders,
            errors=errors,
        )
