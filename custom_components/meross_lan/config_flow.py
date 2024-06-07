"""Config flow for Meross LAN integration."""

import asyncio
from contextlib import contextmanager
from enum import StrEnum
import json
import logging
from time import time
import typing

from homeassistant import config_entries as ce, const as hac
from homeassistant.const import CONF_ERROR
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.selector import selector
import voluptuous as vol

from . import MerossApi, const as mlc
from .helpers import ConfigEntriesHelper, ConfigEntryType, reverse_lookup
from .helpers.manager import CloudApiClient
from .merossclient import (
    HostAddress,
    MerossDeviceDescriptor,
    MerossKeyError,
    cloudapi,
    const as mc,
    fmt_macaddress,
    namespaces as mn,
)
from .merossclient.httpclient import MerossHttpClient
from .merossclient.mqttclient import MerossMQTTDeviceClient

if typing.TYPE_CHECKING:
    from homeassistant.components.dhcp import DhcpServiceInfo
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo

    from .meross_profile import MQTTConnection


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


class MerossFlowHandlerMixin(
    ce.ConfigEntryBaseFlow if typing.TYPE_CHECKING else object
):
    """Mixin providing commons for Config and Option flows"""

    VERSION = 1
    MINOR_VERSION = 1

    _profile_entry: ce.ConfigEntry | None = None
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

    profile_placeholders = {
        "email": "",
        "placeholder": "",
    }

    _is_keyerror: bool = False
    _httpclient: MerossHttpClient | None = None

    # instance properties managed with show_form_errorcontext
    # and async_show_form_with_errors
    _config_schema: dict
    _errors: dict[str, str] | None

    @property
    def api(self):
        return MerossApi.get(self.hass)

    @ce.callback
    def async_abort(self, *, reason: str = "already_configured"):
        return super().async_abort(reason=reason)

    @contextmanager
    def show_form_errorcontext(self):
        """Context manager to catch and show exceptions errors in the user form.
        The CONF_ERROR key will be added as a string label to the UI schema
        containing the exception message so to provide better (untranslated)
        error context."""
        try:
            self._config_schema = {}
            self._errors = None
            yield
        except cloudapi.CloudApiError as error:
            self._errors = {CONF_ERROR: FlowErrorKey.INVALID_AUTH.value}
            self._config_schema = {
                vol.Optional(CONF_ERROR, description={DESCR: str(error)}): str
            }
        except FlowError as error:
            self._errors = {ERR_BASE: error.key.value}
        except Exception as exception:
            self._errors = {CONF_ERROR: FlowErrorKey.CANNOT_CONNECT.value}
            self._config_schema = {
                vol.Optional(
                    CONF_ERROR,
                    description={DESCR: str(exception) or exception.__class__.__name__},
                ): str
            }

    def get_schema_with_errors(self):
        return self._config_schema

    def async_show_form_with_errors(
        self,
        step_id: str,
        *,
        config_schema: dict = {},
        description_placeholders: typing.Mapping[str, str | None] | None = None,
    ):
        """modularize errors managment: use together with show_form_errorcontext and get_schema_with_errors"""
        return super().async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(self._config_schema | config_schema),
            errors=self._errors,
            description_placeholders=description_placeholders,
        )

    def clone_api_diagnostic_config(
        self, config: mlc.DeviceConfigType | mlc.ProfileConfigType
    ):
        """Clone actual MerossApi diagnostic settings on new device/profile config being created."""
        if api_config := self.api.config:
            if mlc.CONF_LOGGING_LEVEL in api_config:
                config[mlc.CONF_LOGGING_LEVEL] = api_config[mlc.CONF_LOGGING_LEVEL]
            if mlc.CONF_OBFUSCATE in api_config:
                config[mlc.CONF_OBFUSCATE] = api_config[mlc.CONF_OBFUSCATE]

    def finish_options_flow(
        self,
        config: mlc.DeviceConfigType | mlc.ProfileConfigType | mlc.HubConfigType,
        reload: bool = False,
    ):
        """Used in OptionsFlow to terminate and exit (with save)."""
        raise NotImplementedError()

    @staticmethod
    def merge_userinput(
        config,
        user_input: dict,
        *nullable_keys,
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
            if key not in user_input:
                config.pop(key, None)
        # just in case it was added to the schema.
        # this also fixes 'dirty' configurations already stored.
        config.pop(CONF_ERROR, None)

    async def async_step_profile(self, user_input=None):
        """configure a Meross cloud profile"""
        # this flow step is really hybrid: it could come from
        # a user flow deciding to create a profile or a user flow
        # when a device key is needed. Or, it could be an OptionFlow
        # for both of the same reasons: either a device key needed
        # or a profile configuration. In any case, we 'force' a bit
        # all of the flows logic and try to directly manage the
        # underlying ConfigEntry in a sort of a crazy generalization
        profile_config = self.profile_config

        with self.show_form_errorcontext():
            if user_input:
                hass = self.hass
                api = self.api
                # profile_config has both user set keys (updated through user_input)
                # and MerossCloudCredentials keys (updated when logging into Meross http api)
                # it also serves as a cache for the UI step and so carries some temporary
                # keys which need to be removed before persisting to config entry
                self.merge_userinput(
                    profile_config,
                    user_input,
                    mlc.CONF_CLOUD_REGION,
                    mlc.CONF_MFA_CODE,
                )
                if (mlc.CONF_PASSWORD in user_input) or (
                    mlc.CONF_MFA_CODE in user_input
                ):
                    # this is setup conditionally, only when login is required in order to
                    # initially create an account (either new profile or device.key_error)
                    # or to manually refresh a token (it would be a profile OptionFlow).
                    # In either cases we're prepared to (optionally) handle MFA.
                    # On first try we're not setting that (we don't ask the user) but
                    # if an MFA error arises we'll repeat the same step ('profile')
                    # with only the mfa code request field (like if it was an optional sub-step)
                    cloudapiclient = CloudApiClient(api)
                    try:
                        credentials = await cloudapiclient.async_signin(
                            profile_config[mlc.CONF_EMAIL],
                            profile_config[mlc.CONF_PASSWORD],  # type: ignore
                            region=user_input.get(mlc.CONF_CLOUD_REGION),
                            domain=profile_config.get(mc.KEY_DOMAIN),
                            mfa_code=user_input.get(mlc.CONF_MFA_CODE),
                        )
                    except cloudapi.CloudApiMfaError as mfa_error:
                        return self.async_show_form(
                            step_id="profile",
                            data_schema=vol.Schema(
                                {
                                    vol.Optional(
                                        CONF_ERROR,
                                        description={DESCR: str(mfa_error)},
                                    ): str,
                                    vol.Required(
                                        mlc.CONF_MFA_CODE,
                                    ): str,
                                }
                            ),
                            errors={CONF_ERROR: FlowErrorKey.INVALID_AUTH.value},
                            description_placeholders=self.profile_placeholders,
                        )
                    if (
                        mc.KEY_USERID_ in profile_config
                        and credentials[mc.KEY_USERID_]
                        != profile_config[mc.KEY_USERID_]
                    ):
                        await cloudapiclient.async_logout_safe()
                        raise FlowError(FlowErrorKey.CLOUD_PROFILE_MISMATCH)
                    # adjust eventual temporary params from config
                    if not profile_config.get(mlc.CONF_SAVE_PASSWORD):
                        profile_config.pop(mlc.CONF_PASSWORD, None)
                    if mlc.CONF_MFA_CODE in profile_config:
                        profile_config[mlc.CONF_MFA_CODE] = True
                    # store the fresh credentials
                    profile_config.update(credentials)  # type: ignore

                if self._profile_entry:
                    # we were managing a profile OptionsFlow: fast save
                    return self.finish_options_flow(profile_config)

                # abort any eventual duplicate progress flow
                # also, even if the user was creating a new profile,
                # updates any eventually existing one...
                # we will eventually abort this flow later
                unique_id = f"profile.{profile_config[mc.KEY_USERID_]}"
                helper = ConfigEntriesHelper(hass)
                profile_flow = helper.get_config_flow(unique_id)
                if profile_flow and (profile_flow["flow_id"] != self.flow_id):
                    helper.config_entries.flow.async_abort(profile_flow["flow_id"])
                profile_entry = helper.get_config_entry(unique_id)
                if profile_entry:
                    helper.config_entries.async_update_entry(
                        profile_entry,
                        data=profile_config,
                    )
                    if not self._is_keyerror:
                        # this flow was creating a profile but it's entry is
                        # already in place (and updated)
                        return self.async_abort()
                else:
                    # this profile config is new either because of keyerror
                    # or user creating a cloud profile.
                    self.clone_api_diagnostic_config(profile_config)
                    if self._is_keyerror:
                        # this flow is managing a device but since the profile
                        # entry is new, we'll directly setup that
                        await helper.config_entries.async_add(
                            # there's a bad compatibility issue between core 2024.1 and
                            # previous versions up to latest 2023 on ConfigEntry. Namely:
                            # previous core versions used positional args in ConfigEntry
                            # while core 2024.X moves to full kwargs with required minor_version
                            # this patch is the best I can think of
                            ce.ConfigEntry(
                                version=self.VERSION,
                                minor_version=self.MINOR_VERSION,  # type: ignore
                                domain=mlc.DOMAIN,
                                title=profile_config[mc.KEY_EMAIL],
                                data=profile_config,
                                options={},  # required since 2024.6
                                source=ce.SOURCE_USER,
                                unique_id=unique_id,
                            )
                            if hac.MAJOR_VERSION >= 2024
                            else ce.ConfigEntry(  # type: ignore
                                version=self.VERSION,
                                domain=mlc.DOMAIN,
                                title=profile_config[mc.KEY_EMAIL],
                                data=profile_config,
                                source=ce.SOURCE_USER,
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

        config_schema = self.get_schema_with_errors()
        if self._profile_entry:
            # this is a profile OptionsFlow
            profile = MerossApi.profiles.get(profile_config[mc.KEY_USERID_])
            require_login = not (profile and profile.token_is_valid)
        else:
            # this is not a profile OptionsFlow so we'd need to login for sure
            # with full credentials
            config_schema[
                vol.Optional(
                    mlc.CONF_CLOUD_REGION,
                    description={DESCR: profile_config.get(mlc.CONF_CLOUD_REGION)},
                )
            ] = selector(
                {
                    "select": {
                        "options": list(cloudapi.API_URL_MAP.keys()),
                        "translation_key": mlc.CONF_CLOUD_REGION,
                        "mode": "dropdown",
                    }
                }
            )
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
            if profile_config.get(mlc.CONF_MFA_CODE):
                # this is when we already have credentials (OptionsFlow then)
                # and those are stating the login was an MFA
                config_schema[vol.Optional(mlc.CONF_MFA_CODE)] = str
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
        if self._profile_entry:
            self._setup_entitymanager_schema(config_schema, profile_config)
        return self.async_show_form_with_errors(
            "profile",
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
        api = self.api
        if key is None:
            key = ""
        if _httpclient := self._httpclient:
            _httpclient.host = host
            _httpclient.key = key
        else:
            self._httpclient = _httpclient = MerossHttpClient(
                host,
                key,
                None,
                api,  # type: ignore (api almost duck-compatible with logging.Logger)
                api.VERBOSE,
            )

        payload = (
            await _httpclient.async_request_strict(
                *mn.Appliance_System_All.request_default
            )
        )[mc.KEY_PAYLOAD]
        payload.update(
            (
                await _httpclient.async_request_strict(
                    *mn.Appliance_System_Ability.request_default
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
        self, device_id: str, key: str | None, descriptor: MerossDeviceDescriptor | None
    ) -> tuple[mlc.DeviceConfigType, MerossDeviceDescriptor]:
        mqttconnections: list[MQTTConnection] = []
        if key is None:
            key = ""
        if descriptor:
            profile = MerossApi.profiles.get(descriptor.userId)  # type: ignore
            if profile and (profile.key == key):
                if profile.allow_mqtt_publish:
                    mqttconnections = await profile.get_or_create_mqttconnections(
                        device_id
                    )
                    if not mqttconnections:
                        raise Exception(
                            f"Meross cloud profile ({profile.config[mc.KEY_EMAIL]}) brokers are unavailable at the moment"
                        )
                else:
                    raise Exception(
                        f"Meross cloud profile ({profile.config[mc.KEY_EMAIL]}) doesn't allow MQTT publishing"
                    )

        if not mqttconnections:
            # this means the device is not Meross cloud binded or the profile
            # is not configured/loaded at least according to our euristics.
            # We'll try HA broker if available
            hamqttconnection = self.api.mqtt_connection
            if not hamqttconnection.mqtt_is_connected:
                raise Exception(
                    "No MQTT broker (either Meross cloud or HA local broker) available to connect"
                )
            mqttconnections.append(hamqttconnection)

        # acrobatic asyncio:
        # we expect only one of the mqttconnections to eventually
        # succesfully identify the device while other will raise
        # exceptions likely due to timeout or malformed responses
        # we'll then wait for the first (and only) success one while
        # eventually collect the exceptions
        exceptions = []
        identifies = asyncio.as_completed(
            {
                mqttconnection.async_identify_device(device_id, key or "")
                for mqttconnection in mqttconnections
            }
        )
        for identify_coro in identifies:
            try:
                device_config = await identify_coro
                return device_config, MerossDeviceDescriptor(
                    device_config[mlc.CONF_PAYLOAD]
                )
            except Exception as exception:
                exceptions.append(exception)

        raise exceptions[0]

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


class ConfigFlow(MerossFlowHandlerMixin, ce.ConfigFlow, domain=mlc.DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""

    DHCP_DISCOVERIES: typing.ClassVar = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """initial step (menu) for user initiated flows"""
        if profile := next(iter(MerossApi.active_profiles()), None):
            self.device_config = {mlc.CONF_KEY: profile.key}  # type: ignore[assignment]
        else:
            self.device_config = {mlc.CONF_KEY: self.api.key}  # type: ignore[assignment]
        self.profile_config = {}  # type: ignore[assignment]
        return self.async_show_menu(
            step_id="user",
            menu_options=["profile", "device"],
        )

    async def async_step_unignore(self, user_input):
        """Rediscover a config entry by it's unique_id."""
        match ConfigEntryType.get_type_and_id(user_input["unique_id"]):
            case (ConfigEntryType.DEVICE, mac_address_fmt):
                if mac_address_fmt in ConfigFlow.DHCP_DISCOVERIES:
                    return await self.async_step_dhcp(
                        ConfigFlow.DHCP_DISCOVERIES.pop(mac_address_fmt)
                    )

        return self.async_abort()

    async def async_step_hub(self, user_input=None):
        """configure the MQTT discovery device key"""
        if user_input is not None:
            return self.async_create_entry(title="MQTT Hub", data=user_input)
        if await self.async_set_unique_id(mlc.DOMAIN):
            return self.async_abort()
        return self.async_show_form(
            step_id="hub",
            data_schema=vol.Schema(
                {
                    vol.Optional(mlc.CONF_KEY): str,
                }
            ),
        )

    async def async_step_device(self, user_input=None):
        """manual device configuration"""
        device_config = self.device_config
        with self.show_form_errorcontext():
            if user_input:
                self.merge_userinput(device_config, user_input, mlc.CONF_KEY)
                try:
                    return await self._async_set_device_config(
                        False,
                        *await self._async_http_discovery(
                            user_input[mlc.CONF_HOST], user_input.get(mlc.CONF_KEY)
                        ),
                    )
                except MerossKeyError:
                    return await self.async_step_keyerror()

        return self.async_show_form_with_errors(
            "device",
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
        self, discovery_info: mlc.DeviceConfigType
    ):
        """
        this is actually the entry point for devices discovered through our MQTTConnection(s)
        """
        return await self._async_set_device_config(
            True,
            discovery_info,
            MerossDeviceDescriptor(discovery_info[mlc.CONF_PAYLOAD]),
        )

    async def async_step_dhcp(self, discovery_info: "DhcpServiceInfo"):
        """Handle a flow initialized by DHCP discovery."""
        api = self.api
        api.log(api.DEBUG, "received dhcp discovery: %s", str(discovery_info))
        host = discovery_info.ip
        macaddress = discovery_info.macaddress
        macaddress_fmt = fmt_macaddress(macaddress)
        # check if the device is already registered
        config_entries = self.hass.config_entries
        try:
            for entry in config_entries.async_entries(mlc.DOMAIN):
                match ConfigEntryType.get_type_and_id(entry.unique_id):
                    case (ConfigEntryType.DEVICE, device_id):
                        if device_id[-12:].lower() != macaddress_fmt:
                            continue
                        if entry.source == ce.SOURCE_IGNORE:
                            ConfigFlow.DHCP_DISCOVERIES[macaddress_fmt] = discovery_info
                            return self.async_abort()
                        entry_data = entry.data
                        entry_descriptor = MerossDeviceDescriptor(
                            entry_data[mlc.CONF_PAYLOAD]
                        )
                        if entry_descriptor.macAddress_fmt != macaddress_fmt:
                            # This is an error though:the check against device_id[-12:]
                            # should have identified this...let it be..
                            continue

                        if entry_data.get(mlc.CONF_HOST) != host:
                            # before updating, check the host ip is 'really' valid
                            try:
                                _device_config, _descriptor = (
                                    await self._async_http_discovery(
                                        host, entry_data.get(mlc.CONF_KEY)
                                    )
                                )
                                if (
                                    _device_config[mlc.CONF_DEVICE_ID]
                                    == entry_data[mlc.CONF_DEVICE_ID]
                                ):
                                    data = dict(entry_data)
                                    data.update(_device_config)
                                    data[mlc.CONF_TIMESTAMP] = (
                                        time()
                                    )  # force ConfigEntry update..
                                    config_entries.async_update_entry(entry, data=data)
                                    api.log(
                                        api.INFO,
                                        "DHCP updated (ip:%s mac:%s) for uuid:%s",
                                        host,
                                        macaddress,
                                        api.loggable_device_id(entry_descriptor.uuid),
                                    )
                                else:
                                    api.log(
                                        api.WARNING,
                                        "received a DHCP update (ip:%s mac:%s) but the new uuid:%s doesn't match the configured one (uuid:%s)",
                                        host,
                                        macaddress,
                                        api.loggable_device_id(_descriptor.uuid),
                                        api.loggable_device_id(entry_descriptor.uuid),
                                    )

                            except Exception as error:
                                api.log(
                                    api.WARNING,
                                    "DHCP update error %s trying to identify uuid:%s at (ip:%s mac:%s)",
                                    str(error),
                                    api.loggable_device_id(entry_descriptor.uuid),
                                    host,
                                    macaddress,
                                )
                        return self.async_abort()

                    case _:
                        continue

        except Exception as exception:
            api.log_exception(api.WARNING, exception, "DHCP update check")

        try:
            # try device identification so the user/UI has a good context to start with
            for profile in MerossApi.active_profiles():
                try:
                    return await self._async_set_device_config(
                        True, *await self._async_http_discovery(host, profile.key)
                    )
                except Exception:
                    pass

            if key := api.key:
                try:
                    return await self._async_set_device_config(
                        True, *await self._async_http_discovery(host, key)
                    )
                except Exception:
                    pass

        except Exception as exception:
            api.log_exception(
                api.DEBUG,
                exception,
                "identifying meross device (ip:%s host:%s mac:%s)",
                host,
                discovery_info.hostname,
                macaddress,
            )
            # forgive and continue if we cant discover the device...let the user work it out

        for progress in config_entries.flow.async_progress_by_handler(
            self.handler,
            include_uninitialized=True,
        ):
            if progress["flow_id"] == self.flow_id:
                continue
            try:
                if progress["context"]["unique_id"] == macaddress_fmt:  # type: ignore
                    config_entries.flow.async_abort(progress["flow_id"])
            except Exception:
                pass

        await self.async_set_unique_id(macaddress_fmt, raise_on_progress=False)
        ConfigFlow.DHCP_DISCOVERIES[macaddress_fmt] = discovery_info
        self._set_flow_title(f"{discovery_info.hostname or host} ({macaddress})")
        self.device_config = {  # type: ignore
            mlc.CONF_HOST: host,
        }
        return await self.async_step_device()

    async def async_step_mqtt(self, discovery_info: "MqttServiceInfo"):
        """manage the MQTT discovery flow"""
        # this entry should only ever called once after startup
        # when HA thinks we're interested in discovery.
        # If our MerossApi is already running it will manage the discovery itself
        # so this flow is only useful when MerossLan has no configuration yet
        # and we leverage the default mqtt discovery to setup our manager
        mqtt_connection = self.api.mqtt_connection
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
        ConfigFlow.DHCP_DISCOVERIES.pop(self.unique_id[-12:].lower(), None)  # type: ignore
        return self.async_create_entry(
            title=self._title,
            data=self.device_config,
        )

    async def _async_set_device_config(
        self,
        is_discovery_step: bool,
        device_config: mlc.DeviceConfigType,
        descriptor: MerossDeviceDescriptor,
    ):
        uuid = descriptor.uuid
        mac_address_fmt = descriptor.macAddress_fmt
        # The approach here is to abort any previous flow for the
        # same uuid/macaddress and keep flowing only the last (current)
        flowmanager = self.hass.config_entries.flow
        for progress in flowmanager.async_progress_by_handler(
            self.handler,
            include_uninitialized=True,
        ):
            if progress["flow_id"] == self.flow_id:
                continue
            try:
                if progress["context"]["unique_id"] in (uuid, mac_address_fmt):  # type: ignore
                    flowmanager.async_abort(progress["flow_id"])
            except Exception:
                pass

        # at this stage (succesful device identification) the flow/entry
        # unique_id is the full uuid (in contrast with dhcp discovery
        # setting just the macaddress). This way we can distinguish progress flows
        # for the same device coming from both DHCP/MQTT with the idea that
        # progress with just the mac are a bit less complete since we're still
        # unable to identify the device
        await self.async_set_unique_id(uuid, raise_on_progress=False)

        self.clone_api_diagnostic_config(device_config)
        self.device_config = device_config
        self.device_placeholders = {
            "device_type": descriptor.productnametype,
            "device_id": uuid,
        }
        if (
            ((profile_id := descriptor.userId) in MerossApi.profiles)
            and (profile := MerossApi.profiles.get(profile_id))
            and (device_info := profile.get_device_info(uuid))
        ):
            devname = device_info.get(mc.KEY_DEVNAME, uuid)
        else:
            devname = uuid
        self._set_flow_title(f"{descriptor.type} - {devname}")
        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self.device_placeholders,
        )

    def _set_flow_title(self, flow_title: str):
        self._title = flow_title
        self.context["title_placeholders"] = {"name": flow_title}


class OptionsFlow(MerossFlowHandlerMixin, ce.OptionsFlow):
    """
    Manage device options configuration
    """

    _MENU_OPTIONS = {
        "hub": ["hub", "diagnostics"],
        "profile": ["profile", "diagnostics"],
        "device": ["device", "diagnostics", "bind", "unbind"],
    }

    config: mlc.HubConfigType | mlc.DeviceConfigType | mlc.ProfileConfigType

    BindConfigType = typing.TypedDict(
        "BindConfigType",
        {
            "domain": str | None,
            "key": str | None,
            "userid": int | None,
        },
    )
    bind_config: BindConfigType

    __slots__ = (
        "config_entry",
        "config_entry_id",
        "config",
        "repair_issue_id",
        "bind_config",
        "bind_placeholders",
    )

    def __init__(
        self,
        config_entry: ce.ConfigEntry,
        repair_issue_id: str | None = None,
    ):
        self.config_entry: typing.Final = config_entry
        self.config_entry_id: typing.Final = config_entry.entry_id
        self.config = dict(self.config_entry.data)  # type: ignore
        self.repair_issue_id = repair_issue_id

    async def async_step_init(self, user_input=None):
        match ConfigEntryType.get_type_and_id(self.config_entry.unique_id):
            case (ConfigEntryType.DEVICE, device_id):
                self.device_config = typing.cast(mlc.DeviceConfigType, self.config)
                if mlc.CONF_TRACE in self.device_config:
                    self.device_config.pop(mlc.CONF_TRACE)  # totally removed in v5.0
                self._device_id = device_id
                assert device_id == self.device_config[mlc.CONF_DEVICE_ID]
                device = MerossApi.devices[device_id]
                # if config not loaded the device is None
                self.device_descriptor = (
                    device.descriptor
                    if device
                    else MerossDeviceDescriptor(self.device_config[mlc.CONF_PAYLOAD])
                )
                self.device_placeholders = {
                    "device_type": self.device_descriptor.productnametype,
                    "device_id": device_id,
                }
                return await self.async_step_menu("device")

            case (ConfigEntryType.PROFILE, _):
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
                return await self.async_step_menu("profile")

            case (ConfigEntryType.HUB, _):
                return await self.async_step_menu("hub")

    async def async_step_menu(self, user_input):
        if self.repair_issue_id:
            return await getattr(self, f"async_step_{user_input}")(None)
        else:
            return self.async_show_menu(
                step_id="menu",
                menu_options=self._MENU_OPTIONS[user_input],
            )

    async def async_step_hub(self, user_input=None):
        hub_config = self.config
        if user_input is not None:
            self.merge_userinput(hub_config, user_input, mlc.CONF_KEY)
            return self.finish_options_flow(hub_config)

        config_schema = {
            vol.Optional(
                mlc.CONF_KEY,
                default="",  # type: ignore
                description={DESCR: hub_config.get(mlc.CONF_KEY)},
            ): str,
            vol.Required(
                mlc.CONF_ALLOW_MQTT_PUBLISH,
                description={DESCR: hub_config.get(mlc.CONF_ALLOW_MQTT_PUBLISH, True)},
            ): bool,
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
        device = MerossApi.devices[self._device_id]
        device_config = self.device_config

        with self.show_form_errorcontext():
            if user_input is not None:
                self.merge_userinput(
                    device_config, user_input, mlc.CONF_KEY, mlc.CONF_HOST
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
                                self._device_id, _key, self.device_descriptor
                            )
                        except Exception as e:
                            inner_exception = e
                    if _conf_protocol is not mlc.CONF_PROTOCOL_MQTT:
                        if _try_host := (_host or self.device_descriptor.innerIp):
                            try:
                                (
                                    device_config_update,
                                    descriptor_update,
                                ) = await self._async_http_discovery(_try_host, _key)
                            except Exception as e:
                                inner_exception = e

                    if not device_config_update or not descriptor_update:
                        raise inner_exception or FlowError(FlowErrorKey.CANNOT_CONNECT)
                    if self._device_id != device_config_update[mlc.CONF_DEVICE_ID]:
                        raise FlowError(OptionsFlowErrorKey.DEVICE_ID_MISMATCH)
                    device_config[mlc.CONF_PAYLOAD] = device_config_update[
                        mlc.CONF_PAYLOAD
                    ]
                    if device:
                        try:
                            await device.async_entry_option_update(user_input)
                        except Exception:
                            pass  # forgive any error

                    # cleanup keys which might wrongly have been persisted
                    device_config.pop(mlc.CONF_CLOUD_KEY, None)
                    device_config.pop(mc.KEY_TIMEZONE, None)

                    if self.config_entry.state == ce.ConfigEntryState.SETUP_ERROR:
                        api = self.api
                        try:  # to fix the device registry in case it was corrupted by #341
                            dev_reg = MerossApi.get_device_registry()
                            device_identifiers = {(str(mlc.DOMAIN), self._device_id)}
                            device_entry = dev_reg.async_get_device(
                                identifiers=device_identifiers
                            )
                            if device_entry and (
                                len(device_entry.connections) > 1
                                or len(device_entry.config_entries) > 1
                            ):
                                _area_id = device_entry.area_id
                                _name_by_user = device_entry.name_by_user
                                dev_reg.async_remove_device(device_entry.id)
                                dev_reg.async_get_or_create(
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
                                api.log(
                                    api.WARNING,
                                    "Device registry entry for %s (uuid:%s) was updated in order to fix it. The friendly name ('%s') has been lost and needs to be manually re-entered",
                                    descriptor_update.productmodel,
                                    api.loggable_device_id(self._device_id),
                                    _name_by_user,
                                )

                        except Exception as error:
                            api.log(
                                api.WARNING,
                                "error (%s) while trying to repair device registry for %s (uuid:%s)",
                                str(error),
                                descriptor_update.productmodel,
                                api.loggable_device_id(self._device_id),
                            )
                        return self.finish_options_flow(device_config, True)

                    return self.finish_options_flow(device_config)

                except MerossKeyError:
                    return await self.async_step_keyerror()

            else:
                _host = device_config.get(mlc.CONF_HOST)
                _key = device_config.get(mlc.CONF_KEY)

        self.device_placeholders["host"] = _host or "MQTT"
        config_schema = self.get_schema_with_errors()
        config_schema |= {
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
                await device.async_entry_option_setup(config_schema)
            except Exception:
                pass  # forgive any error

        self._setup_entitymanager_schema(config_schema, device_config)
        return self.async_show_form_with_errors(
            "device",
            description_placeholders=self.device_placeholders,
        )

    async def async_step_diagnostics(self, user_input=None):
        # when choosing to start a diagnostic from the OptionsFlow UI we'll
        # reload the entry so we trace also the full initialization process
        # for a more complete insight on the EntityManager context.
        # The info to trigger the trace_open on entry setup is carried through
        # the global MerossApi.managers_transient_state
        config = self.config
        if user_input:
            config[mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES] = user_input[
                mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES
            ]
            config[mlc.CONF_LOGGING_LEVEL] = (
                reverse_lookup(
                    mlc.CONF_LOGGING_LEVEL_OPTIONS, user_input[mlc.CONF_LOGGING_LEVEL]
                )
                or logging.NOTSET
            )
            config[mlc.CONF_OBFUSCATE] = user_input[mlc.CONF_OBFUSCATE]
            config[mlc.CONF_TRACE_TIMEOUT] = user_input.get(mlc.CONF_TRACE_TIMEOUT)
            if user_input[mlc.CONF_TRACE]:
                # only reload and start tracing if the user wish so
                state = MerossApi.managers_transient_state.setdefault(
                    self.config_entry_id, {}
                )
                state[mlc.CONF_TRACE] = user_input[mlc.CONF_TRACE]
                return self.finish_options_flow(config, True)
            return self.finish_options_flow(config)

        config_schema = {
            vol.Required(
                mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES,
                description={
                    DESCR: config.get(mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES, False)
                },
            ): bool,
            vol.Required(
                mlc.CONF_LOGGING_LEVEL,
                description={
                    DESCR: mlc.CONF_LOGGING_LEVEL_OPTIONS.get(
                        config.get(mlc.CONF_LOGGING_LEVEL, logging.NOTSET), "default"
                    )
                },
            ): selector(
                {
                    "select": {
                        "options": list(mlc.CONF_LOGGING_LEVEL_OPTIONS.values()),
                        "translation_key": mlc.CONF_LOGGING_LEVEL,
                        "mode": "dropdown",
                    }
                }
            ),
            vol.Required(
                mlc.CONF_OBFUSCATE,
                description={DESCR: config.get(mlc.CONF_OBFUSCATE, True)},
            ): bool,
            vol.Required(
                mlc.CONF_TRACE,
                default=False,  # type: ignore
            ): bool,
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
        with self.show_form_errorcontext():
            if user_input:
                hass = self.hass
                api = self.api
                bind_config = self.bind_config
                bind_config[mc.KEY_DOMAIN] = domain = user_input.get(mc.KEY_DOMAIN)
                bind_config[mc.KEY_KEY] = key = user_input.get(mc.KEY_KEY)
                bind_config[mc.KEY_USERID_] = userid = user_input.get(mc.KEY_USERID_)

                if domain:
                    broker_address = HostAddress.build(domain, 8883)
                else:
                    mqtt_connection = api.mqtt_connection
                    if not mqtt_connection.mqtt_is_connected:
                        raise FlowError(OptionsFlowErrorKey.HABROKER_NOT_CONNECTED)
                    broker_address = mqtt_connection.broker
                    if broker_address.port == 1883:
                        broker_address.port = 8883
                # set back the value so the user has an hint in case of errors connecting
                bind_config[mc.KEY_DOMAIN] = domain = str(broker_address)
                # we have to check the broker address is a network bound IPV4 address
                # since localhost would have no meaning (or a wrong one) in the device
                import socket

                addrinfos = socket.getaddrinfo(
                    socket.getfqdn(broker_address.host),
                    broker_address.port,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                    proto=socket.IPPROTO_TCP,
                )
                # addrinfos contains the resolved ipv4 address(es) weather or not
                # our broker_address.host is an ipv6 or ipv4 host name/addr
                for addrinfo in addrinfos:
                    # addrinfo: (family, type, proto, canonname, sockaddr)
                    if addrinfo[4][0] == "127.0.0.1":
                        # localhost could work in our mqtt.Client check but
                        # it will not when configured in the device bind
                        # so we're trying to resolve it to a valid network name
                        from homeassistant.helpers.network import get_url
                        import yarl

                        hasslocalhost = yarl.URL(get_url(hass, allow_ip=True)).host
                        if not hasslocalhost:
                            raise FlowError(FlowErrorKey.CANNOT_CONNECT)
                        broker_address.host = hasslocalhost
                        bind_config[mc.KEY_DOMAIN] = domain = str(broker_address)
                        break

                device = MerossApi.devices[self._device_id]
                if not (device and device.online):
                    raise FlowError(FlowErrorKey.CANNOT_CONNECT)

                key = key or api.key or ""
                userid = "" if userid is None else str(userid)
                mqttclient = MerossMQTTDeviceClient(
                    device.id, key=key, userid=userid, loop=hass.loop
                )
                if api.isEnabledFor(api.VERBOSE):
                    mqttclient.enable_logger(api)  # type: ignore (Loggable is duck-compatible with Logger)
                try:
                    await asyncio.wait_for(
                        await mqttclient.async_connect(broker_address), 5
                    )
                finally:
                    await mqttclient.async_shutdown()

                device.log(device.DEBUG, "Initiating MQTT binding to %s", domain)
                response = await device.async_bind(
                    broker_address, key=key, userid=userid
                )
                if (
                    response
                    and response[mc.KEY_HEADER][mc.KEY_METHOD] == mc.METHOD_SETACK
                ):
                    device.log(device.INFO, "MQTT binding to %s was succesfull", domain)
                    device_config = self.device_config
                    device_config[mlc.CONF_KEY] = key
                    # the device config needs to be updated too. This is not critical
                    # since the device, when onlining will refresh this but we have a chance to speed
                    # up the process so when it restart it'll be already updated
                    if host := device.host:
                        try:
                            (
                                device_config_update,
                                descriptor_update,
                            ) = await self._async_http_discovery(host, key)
                            device_config[mlc.CONF_PAYLOAD] = device_config_update[
                                mlc.CONF_PAYLOAD
                            ]
                        except Exception:
                            pass
                    hass.config_entries.async_update_entry(
                        self.config_entry, data=device_config
                    )
                    return self.async_show_form(
                        step_id="bind_finalize",
                        data_schema=vol.Schema({}),
                        description_placeholders={"domain": domain},
                    )
                else:
                    device.log(device.DEBUG, "MQTT binding to %s has failed", domain)

                raise FlowError(FlowErrorKey.CANNOT_CONNECT)
            else:
                self.bind_config = {
                    mc.KEY_DOMAIN: None,
                    mc.KEY_KEY: None,
                    mc.KEY_USERID_: None,
                }
                bind_config = self.bind_config
                self.bind_placeholders = {
                    "domain": str(next(iter(self.device_descriptor.brokers), None))
                }

        return self.async_show_form_with_errors(
            "bind",
            config_schema={
                vol.Optional(
                    mc.KEY_DOMAIN, description={DESCR: bind_config[mc.KEY_DOMAIN]}
                ): str,
                vol.Optional(
                    mc.KEY_KEY, description={DESCR: bind_config[mc.KEY_KEY]}
                ): str,
                vol.Optional(
                    mc.KEY_USERID_, description={DESCR: bind_config[mc.KEY_USERID_]}
                ): cv.positive_int,
            },
            description_placeholders=self.bind_placeholders,
        )

    async def async_step_bind_finalize(self, user_input=None):
        ConfigEntriesHelper(self.hass).schedule_reload(self.config_entry_id)
        return self.async_create_entry(data=None)  # type: ignore

    async def async_step_unbind(self, user_input=None):
        KEY_ACTION = "post_action"
        KEY_ACTION_DISABLE = "disable"
        KEY_ACTION_DELETE = "delete"

        with self.show_form_errorcontext():
            if user_input:
                device = MerossApi.devices[self._device_id]
                if not (device and device.online):
                    raise FlowError(FlowErrorKey.CANNOT_CONNECT)

                await device.async_unbind()
                action = user_input[KEY_ACTION]
                hass = self.hass
                if action == KEY_ACTION_DISABLE:
                    hass.async_create_task(
                        hass.config_entries.async_set_disabled_by(
                            self.config_entry_id,
                            ce.ConfigEntryDisabler.USER,
                        )
                    )
                elif action == KEY_ACTION_DELETE:
                    hass.async_create_task(
                        hass.config_entries.async_remove(self.config_entry_id)
                    )
                return self.async_create_entry(data=None)  # type: ignore

        return self.async_show_form_with_errors(
            "unbind",
            config_schema={
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
            },
        )

    def finish_options_flow(
        self,
        config: mlc.DeviceConfigType | mlc.ProfileConfigType | mlc.HubConfigType,
        reload: bool = False,
    ):
        """Used in OptionsFlow to terminate and exit (with save)."""
        self.hass.config_entries.async_update_entry(self.config_entry, data=config)
        if reload:
            ConfigEntriesHelper(self.hass).schedule_reload(self.config_entry_id)
        return self.async_create_entry(data=None)  # type: ignore
