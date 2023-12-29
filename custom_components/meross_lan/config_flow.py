"""Config flow for Meross LAN integration."""
from __future__ import annotations

from contextlib import contextmanager
import json
from logging import DEBUG
from time import time
import typing

from homeassistant import config_entries
from homeassistant.const import CONF_ERROR
from homeassistant.data_entry_flow import AbortFlow, FlowHandler, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import selector
import voluptuous as vol

from . import MerossApi, const as mlc
from .helpers import LOGGER, ApiProfile, ConfigEntriesHelper, StrEnum
from .merossclient import (
    MerossDeviceDescriptor,
    MerossKeyError,
    const as mc,
    get_default_arguments,
    parse_host_port,
)
from .merossclient.cloudapi import (
    CloudApiError,
    async_cloudapi_login,
    async_cloudapi_logout_safe,
)
from .merossclient.httpclient import MerossHttpClient
from .merossclient.mqttclient import MerossMQTTDeviceClient

if typing.TYPE_CHECKING:
    from typing import Final

    from homeassistant.components.dhcp import DhcpServiceInfo
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo


# helper conf keys not persisted to config
DESCR = "suggested_value"
ERR_BASE = "base"


class FlowErrorKey(StrEnum):
    """These error keys are common to both Config and Options flows"""

    ALREADY_CONFIGURED_DEVICE = "already_configured_device"
    CANNOT_CONNECT = "cannot_connect"
    CLOUD_PROFILE_MISMATCH = "cloud_profile_mismatch"
    INVALID_AUTH = "invalid_auth"
    INVALID_KEY = "invalid_key"
    INVALID_NULL_KEY = "invalid_nullkey"


class ConfigFlowErrorKey(StrEnum):
    pass


class OptionsFlowErrorKey(StrEnum):
    DEVICE_ID_MISMATCH = "device_id_mismatch"
    HABROKER_NOT_CONNECTED = "habroker_not_connected"


class FlowError(Exception):
    def __init__(self, key: FlowErrorKey | ConfigFlowErrorKey | OptionsFlowErrorKey):
        super().__init__(key)
        self.key = key


class MerossFlowHandlerMixin(FlowHandler if typing.TYPE_CHECKING else object):
    """Mixin providing commons for Config and Option flows"""

    _profile_entry: config_entries.ConfigEntry | None = None
    """
    This is set when processing a 'profile' OptionsFlow. It is needed
    to discriminate the context in the general purpose 'async_step_profile' since
    that step might come in these scenarios:
    - user initiated (and auto-discovery) profile ConfigFlow
    - user initiated profile OptionsFlow (this is the step that 'fixes' the _profile_entry)
    - intermediate flow step when configuring a device (either ConfigFlow or OptionsFlow)
    in the latter case, the 'async_step_profile' will smartly create/edit the configuration
    entry for the profile which is not the actual entry (a device one) under configuration/edit
    """

    # These values are just buffers for UI state persistance
    device_config: mlc.DeviceConfigType
    profile_config: mlc.ProfileConfigType
    device_descriptor: MerossDeviceDescriptor

    device_placeholders = {
        "device_type": "",
        "device_id": "",
        "host": "",
    }

    profile_placeholders = {}

    _is_keyerror: bool = False
    _httpclient: MerossHttpClient | None = None

    _errors: dict[str, str] | None = None
    _conf_error: str | None = None

    @callback
    def async_abort(self, *, reason: str = "already_configured"):
        return super().async_abort(reason=reason)

    @contextmanager
    def show_form_errorcontext(self):
        try:
            self._errors = None
            self._conf_error = None
            yield
        except CloudApiError as error:
            self._errors = {CONF_ERROR: FlowErrorKey.INVALID_AUTH.value}
            self._conf_error = str(error)
        except FlowError as error:
            self._errors = {ERR_BASE: error.key.value}
            self.conf_error = None
        except Exception as exception:
            self._errors = {CONF_ERROR: FlowErrorKey.CANNOT_CONNECT.value}
            self._conf_error = f"{exception.__class__.__name__}({str(exception)})"

    def async_show_form_with_errors(
        self,
        step_id: str,
        config_schema: dict | None = None,
        description_placeholders: typing.Mapping[str, str | None] | None = None,
    ):
        """modularize errors managment: use together with flowerrorcontext"""
        if self._conf_error:
            if config_schema:
                # recreate to put the CONF_ERROR at the top of the form
                config_schema = {
                    vol.Optional(
                        CONF_ERROR, description={DESCR: self._conf_error}
                    ): str,
                } | config_schema
            else:
                config_schema = {
                    vol.Optional(CONF_ERROR, description={DESCR: self._conf_error}): str
                }

        return super().async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(config_schema),
            errors=self._errors,
            description_placeholders=description_placeholders,
        )

    def finish_options_flow(
        self, config: mlc.DeviceConfigType | mlc.ProfileConfigType | mlc.HubConfigType
    ):
        """Used in OptionsFlow to terminate and exit (with save)."""
        raise NotImplementedError()

    @staticmethod
    def merge_userinput(
        config,
        user_input: dict,
        nullable_keys: typing.Iterable[str],
    ):
        """
        (dict) merge user_input into the current configuration taking care of
        (damn unsupported) string empty values that HA frontend keeps returning
        as 'no keys' in the payload. This in turn will let the dict.update to
        not update these keys (i.e. remove them or set to None..whatever).
        If we could force HA to return the keys as needed this would be unnecessary
        but I've found no way to tell HA UI to accempt an empty string unless
        I set the key declaration as vol.Optional() = str
        """
        config.update(user_input)
        for key in nullable_keys:
            if key not in user_input and key in config:
                config.pop(key)

    async def async_step_profile(self, user_input=None):
        """configure a Meross cloud profile"""
        profile = None
        profile_config = self.profile_config

        with self.show_form_errorcontext():
            if user_input:
                # profile_config has both user set keys (updated through user_input)
                # and MerossCloudCredentials keys (updated when logging into Meross http api)
                self.merge_userinput(profile_config, user_input, ())
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
                        raise FlowError(FlowErrorKey.CLOUD_PROFILE_MISMATCH)
                    profile_config.update(credentials)  # type: ignore

                if self._profile_entry:
                    # we were managing a profile OptionsFlow: fast save
                    return self.finish_options_flow(profile_config)

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

        config_schema = {}
        if self._profile_entry:
            # this is a profile OptionsFlow
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
                vol.Required(
                    mlc.CONF_SAVE_PASSWORD,
                    description={
                        DESCR: profile_config.get(mlc.CONF_SAVE_PASSWORD, False)
                    },
                )
            ] = bool
        config_schema[
            vol.Required(
                mlc.CONF_ALLOW_MQTT_PUBLISH,
                description={
                    DESCR: profile_config.get(mlc.CONF_ALLOW_MQTT_PUBLISH, False)
                },
            )
        ] = bool
        config_schema[
            vol.Required(
                mlc.CONF_CHECK_FIRMWARE_UPDATES,
                description={
                    DESCR: profile_config.get(mlc.CONF_CHECK_FIRMWARE_UPDATES, False)
                },
            )
        ] = bool
        config_schema[
            vol.Required(
                mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES,
                description={
                    DESCR: profile_config.get(
                        mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES, False
                    )
                },
            )
        ] = bool
        if self._profile_entry:
            self._setup_entitymanager_schema(config_schema, profile_config)

        return self.async_show_form_with_errors(
            step_id="profile",
            config_schema=config_schema,
            description_placeholders=self.profile_placeholders,
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
        mqttconnections = []
        # TODO: we should better detect if the profile is a Meross one
        # and eventually raise a better exception stating if it's available
        # or not (disabled maybe)
        if profile_id and (profile_id in MerossApi.profiles):
            profile = MerossApi.profiles[profile_id]
            if not profile:
                raise Exception(
                    "Unable to identify device over MQTT since Meross cloud profile is disabled"
                )
            if not profile.allow_mqtt_publish:
                raise Exception(
                    "Unable to identify device over MQTT since Meross cloud profile doesn't allow MQTT publishing"
                )
            mqttconnections = profile.get_or_create_mqttconnections(device_id)
        else:
            mqttconnections = [MerossApi.get(self.hass).mqtt_connection]

        for mqttconnection in mqttconnections:
            if device_config := await mqttconnection.async_identify_device(
                device_id, key or ""
            ):
                return device_config, MerossDeviceDescriptor(
                    device_config[mlc.CONF_PAYLOAD]
                )

        raise Exception(
            "No MQTT response: either no available broker or invalid device id"
        )

    def _setup_entitymanager_schema(
        self,
        config_schema: dict,
        config: mlc.DeviceConfigType | mlc.ProfileConfigType | mlc.HubConfigType,
    ):
        """
        Fills (the bottom of) the schema presented to the UI with common settings
        available for all (or almost) the config flows (properties typically configuring
        the EntityManager base class).
        """


class ConfigFlow(MerossFlowHandlerMixin, config_entries.ConfigFlow, domain=mlc.DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        self.device_config = {}  # type: ignore[assignment]
        self.profile_config = {}  # type: ignore[assignment]
        return self.async_show_menu(
            step_id="user",
            menu_options=["profile", "device"],
        )

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
        """manual device configuration"""
        device_config = self.device_config
        with self.show_form_errorcontext():
            if user_input:
                self.merge_userinput(device_config, user_input, (mlc.CONF_KEY))
                try:
                    return await self._async_set_device_config(
                        *await self._async_http_discovery(
                            user_input[mlc.CONF_HOST], user_input.get(mlc.CONF_KEY)
                        )
                    )
                except MerossKeyError:
                    return await self.async_step_keyerror()
            else:
                if profile := next(iter(ApiProfile.active_profiles()), None):
                    device_config[mlc.CONF_KEY] = profile.key

        return self.async_show_form_with_errors(
            step_id="device",
            config_schema={
                vol.Required(
                    mlc.CONF_HOST,
                    description={DESCR: device_config.get(mlc.CONF_HOST)},
                ): str,
                vol.Optional(
                    mlc.CONF_KEY,
                    description={DESCR: device_config.get(mlc.CONF_KEY)},
                ): str,
            },
            description_placeholders=self.device_placeholders,
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

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo):
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

    async def async_step_mqtt(self, discovery_info: MqttServiceInfo):
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
        self.device_placeholders = {
            "device_type": descriptor.productnametype,
            "device_id": device_id,
        }
        if await self.async_set_unique_id(device_id):
            raise AbortFlow("already_configured")

        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self.device_placeholders,
        )


class OptionsFlow(MerossFlowHandlerMixin, config_entries.OptionsFlow):
    """
    Manage device options configuration
    """

    config: mlc.HubConfigType | mlc.DeviceConfigType | mlc.ProfileConfigType

    __slots__ = (
        "config_entry",
        "config_entry_id",
        "config",
    )

    def __init__(self, config_entry: config_entries.ConfigEntry):
        self.config_entry: Final = config_entry
        self.config_entry_id: Final = config_entry.entry_id
        self.config = dict(self.config_entry.data)  # type: ignore

    async def async_step_init(self, user_input=None):
        unique_id = self.config_entry.unique_id
        if unique_id == mlc.DOMAIN:
            return await self.async_step_menu(["hub", "diagnostics"])

        unique_id = unique_id.split(".")  # type: ignore
        if unique_id[0] == "profile":
            self._profile_entry = self.config_entry
            self.profile_config = self.config  # type: ignore
            self.profile_placeholders = {
                "email": self.profile_config.get(mlc.CONF_EMAIL),
                "placeholder": json.dumps(
                    {
                        key: self.profile_config.get(key)
                        for key in (mc.KEY_USERID_, mlc.CONF_KEY)
                    },
                    indent=2,
                ),
            }
            return await self.async_step_menu(["profile", "diagnostics"])

        self.device_config = typing.cast(mlc.DeviceConfigType, self.config)
        if mlc.CONF_TRACE in self.device_config:
            self.device_config.pop(mlc.CONF_TRACE)  # totally removed in v5.0
        self._device_id = unique_id[0]
        assert self._device_id == self.device_config.get(mlc.CONF_DEVICE_ID)
        device = ApiProfile.devices[self._device_id]
        # if config not loaded the device is None
        self.device_descriptor = (
            device.descriptor
            if device
            else MerossDeviceDescriptor(self.device_config.get(mlc.CONF_PAYLOAD))
        )
        self.device_placeholders = {
            "device_type": self.device_descriptor.productnametype,
            "device_id": self._device_id,
        }
        return await self.async_step_menu(["device", "diagnostics", "bind", "unbind"])

    async def async_step_menu(self, user_input=[]):
        return self.async_show_menu(
            step_id="menu",
            menu_options=user_input,
        )

    async def async_step_hub(self, user_input=None):
        hub_config = self.config
        if user_input is not None:
            hub_config[mlc.CONF_KEY] = user_input.get(mlc.CONF_KEY)
            return self.finish_options_flow(hub_config)

        config_schema = {
            vol.Optional(
                mlc.CONF_KEY,
                default="",  # type: ignore
                description={DESCR: hub_config.get(mlc.CONF_KEY)},
            ): str
        }
        self._setup_entitymanager_schema(config_schema, hub_config)
        return self.async_show_form(
            step_id="hub", data_schema=vol.Schema(config_schema)
        )

    async def async_step_device(self, user_input=None):
        """
        general (common) device configuration allowing key set and
        general parameters to be entered/modified
        """
        device = ApiProfile.devices[self._device_id]
        device_config = self.device_config

        with self.show_form_errorcontext():
            if user_input is not None:
                self.merge_userinput(
                    device_config, user_input, (mlc.CONF_KEY, mlc.CONF_HOST)
                )
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
                        raise inner_exception or FlowError(FlowErrorKey.CANNOT_CONNECT)
                    if self._device_id != device_config_update[mlc.CONF_DEVICE_ID]:
                        raise FlowError(OptionsFlowErrorKey.DEVICE_ID_MISMATCH)
                    device_config[mlc.CONF_PAYLOAD] = device_config_update[
                        mlc.CONF_PAYLOAD
                    ]
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
                        self.config_entry, data=device_config
                    )
                    if (
                        self.config_entry.state
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
                                    config_entry_id=self.config_entry.entry_id,
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
                                self._device_id,
                            )

                        await self.hass.config_entries.async_reload(
                            self.config_entry.entry_id
                        )
                    # return None in data so the async_update_entry is not called for the
                    # options to be updated. This will offend the type-checker tho and
                    # it appears as a very dirty trick to HA: beware!
                    return self.async_create_entry(data=None)  # type: ignore

                except MerossKeyError:
                    return await self.async_step_keyerror()

            else:
                _host = device_config.get(mlc.CONF_HOST)
                _key = device_config.get(mlc.CONF_KEY)

        self.device_placeholders["host"] = _host or "MQTT"
        config_schema = {
            vol.Optional(mlc.CONF_HOST, description={DESCR: _host}): str,
            vol.Optional(mlc.CONF_KEY, description={DESCR: _key}): str,
            vol.Required(
                mlc.CONF_PROTOCOL,
                default=mlc.CONF_PROTOCOL_AUTO,  # type: ignore
                description={DESCR: device_config.get(mlc.CONF_PROTOCOL)},
            ): vol.In(mlc.CONF_PROTOCOL_OPTIONS.keys()),
            vol.Required(
                mlc.CONF_POLLING_PERIOD,
                default=mlc.CONF_POLLING_PERIOD_DEFAULT,  # type: ignore
                description={DESCR: device_config.get(mlc.CONF_POLLING_PERIOD)},
            ): cv.positive_int,
        }
        # setup device specific config right before last option
        if device:
            try:
                device.entry_option_setup(config_schema)
            except Exception:
                pass  # forgive any error

        self._setup_entitymanager_schema(config_schema, device_config)
        return self.async_show_form_with_errors(
            step_id="device",
            config_schema=config_schema,
            description_placeholders=self.device_placeholders,
        )

    async def async_step_diagnostics(self, user_input=None):
        # when choosing to start a diagnostic from the OptionsFlow UI we'll
        # reload the entry so we trace also the full initialization process
        # for a more complete insight on the EntityManager context.
        # The info to trigger the trace_open on entry setup is carried through
        # the global ApiProfile.managers_transient_state
        config = self.config
        if user_input:
            config[mlc.CONF_TRACE_TIMEOUT] = user_input.get(mlc.CONF_TRACE_TIMEOUT)

            state = ApiProfile.managers_transient_state.setdefault(
                self.config_entry_id, {}
            )
            state[mlc.CONF_TRACE] = True
            # taskerize the reload so the entry get updated first
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry_id)
            )

            return self.finish_options_flow(config)

        config_schema = {
            vol.Optional(
                mlc.CONF_TRACE_TIMEOUT,
                default=mlc.CONF_TRACE_TIMEOUT_DEFAULT,  # type: ignore
                description={DESCR: config.get(mlc.CONF_TRACE_TIMEOUT)},
            ): cv.positive_int,
        }
        return self.async_show_form(
            step_id="diagnostics", data_schema=vol.Schema(config_schema)
        )

    async def async_step_bind(self, user_input=None):
        KEY_BROKER = "broker"
        KEY_CHECK = "check"
        KEY_KEY = mlc.CONF_KEY
        KEY_USERID = "userid"

        with self.show_form_errorcontext():
            if user_input:
                broker = user_input.get(KEY_BROKER)
                check = user_input.get(KEY_CHECK)
                key = user_input.get(KEY_KEY) or ""
                userid = user_input.get(KEY_USERID)

                if broker:
                    host, port = parse_host_port(broker, 8883)
                else:
                    mqtt_connection = MerossApi.get(self.hass).mqtt_connection
                    if not mqtt_connection.mqtt_is_connected:
                        raise FlowError(OptionsFlowErrorKey.HABROKER_NOT_CONNECTED)
                    host, port = mqtt_connection.broker
                    if port == 1883:
                        port = 8883
                # set back the value so the user has an hint in case of errors connecting
                broker = f"{host}:{port}"

                device = ApiProfile.devices[self._device_id]
                if not (device and device.online):
                    raise FlowError(FlowErrorKey.CANNOT_CONNECT)

                mqttclient = MerossMQTTDeviceClient(
                    device.id,
                    key=key,
                    userid="" if userid is None else str(userid),
                )
                try:
                    await self.hass.async_add_executor_job(
                        mqttclient.connect, host, port
                    )
                finally:
                    mqttclient.safe_disconnect()

                response = await device.async_bind(host, port, key=key, userid=userid)
                if (
                    response
                    and response[mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_SETACK
                ):
                    # binding succesful..update the key in case
                    device_config = self.device_config
                    device_config[mlc.CONF_KEY] = key
                    return self.finish_options_flow(device_config)

                raise FlowError(FlowErrorKey.CANNOT_CONNECT)

            else:
                broker = None
                check = True
                key = self.device_config.get(mlc.CONF_KEY)
                userid = None

        config_schema = {
            vol.Optional(KEY_BROKER, description={DESCR: broker}): str,
            vol.Required(KEY_CHECK, description={DESCR: check}): bool,
            vol.Optional(KEY_KEY, description={DESCR: key}): str,
            vol.Optional(KEY_USERID, description={DESCR: userid}): cv.positive_int,
        }
        return self.async_show_form_with_errors(
            step_id="bind", config_schema=config_schema
        )

    async def async_step_unbind(self, user_input=None):
        KEY_ACTION = "post_action"
        KEY_ACTION_DISABLE = "disable"
        KEY_ACTION_DELETE = "delete"

        with self.show_form_errorcontext():
            if user_input:
                device = ApiProfile.devices[self._device_id]
                if not (device and device.online):
                    raise FlowError(FlowErrorKey.CANNOT_CONNECT)

                device.unbind()
                action = user_input[KEY_ACTION]
                if action == KEY_ACTION_DISABLE:
                    self.hass.async_create_task(
                        self.hass.config_entries.async_set_disabled_by(
                            self.config_entry_id,
                            config_entries.ConfigEntryDisabler.USER,
                        )
                    )
                elif action == KEY_ACTION_DELETE:
                    self.hass.async_create_task(
                        self.hass.config_entries.async_remove(self.config_entry_id)
                    )
                return self.async_create_entry(data=None)  # type: ignore

        config_schema = {
            vol.Required(
                KEY_ACTION,
                default=KEY_ACTION_DISABLE,  # type: ignore
            ): selector(
                {
                    "select": {
                        "options": [KEY_ACTION_DISABLE, KEY_ACTION_DELETE],
                        "translation_key": "unbind_post_action",
                    }
                }
            )
        }
        return self.async_show_form_with_errors(
            step_id="unbind", config_schema=config_schema
        )

    def finish_options_flow(
        self, config: mlc.DeviceConfigType | mlc.ProfileConfigType | mlc.HubConfigType
    ):
        """Used in OptionsFlow to terminate and exit (with save)."""
        self.hass.config_entries.async_update_entry(self.config_entry, data=config)
        return self.async_create_entry(data=None)  # type: ignore
