"""Config flow for Meross LAN integration."""

import asyncio
from contextlib import contextmanager
import enum
from functools import cached_property
import json
import logging
import re
from time import time
from types import MappingProxyType
from typing import TYPE_CHECKING

from homeassistant import config_entries as ce
from homeassistant.const import CONF_ERROR
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    selector,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from . import const as mlc
from .helpers import (
    ConfigEntryType,
    get_default_no_verify_ssl_context,
    reverse_lookup,
)
from .helpers.component_api import ComponentApi
from .helpers.mqtt_profile import MQTTConnection
from .merossclient import (
    HostAddress,
    MerossDeviceDescriptor,
    Transport,
    cloudapi,
    fmt_macaddress,
)
from .merossclient.httpclient import HttpClient
from .merossclient.mqttclient import MQTTDeviceClient
from .merossclient.protocol import (
    MerossKeyError,
    const as mc,
    namespaces as mn,
)

if TYPE_CHECKING:
    from typing import Any, ClassVar, Final, Mapping, NotRequired, TypedDict

    from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
    from homeassistant.helpers.service_info.bluetooth import BluetoothServiceInfo
    from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
    from homeassistant.helpers.service_info.mqtt import MqttServiceInfo

    from .helpers.device import Device
    from .helpers.manager import ConfigEntryManager
    from .helpers.mqtt_profile import MQTTConnection
    from .merossclient import MerossClient
    from .merossclient.protocol import types as mt


class FlowErrorKey(enum.StrEnum):
    ALREADY_CONFIGURED = "already_configured"
    ALREADY_CONFIGURED_DEVICE = "already_configured_device"
    CANNOT_CONNECT = "cannot_connect"
    CLOUD_PROFILE_MISMATCH = "cloud_profile_mismatch"
    INVALID_AUTH = "invalid_auth"
    INVALID_KEY = "invalid_key"
    INVALID_NULL_KEY = "invalid_nullkey"
    DEVICE_ID_MISMATCH = "device_id_mismatch"
    HABROKER_NOT_CONNECTED = "habroker_not_connected"
    BROKER_ADDRESS_INVALID = "broker_address_invalid"
    BROKER_CONNECTION_ERROR = "broker_connection_error"


class FlowError(Exception):
    def __init__(self, key: FlowErrorKey):
        super().__init__(key)
        self.key = key


def _optional(key: str, config: "Mapping | None", default=None) -> vol.Marker:
    return vol.Optional(
        key,
        description={
            "suggested_value": (config and config.get(key, default)) or default
        },
    )


def _required(key: str, config: "Mapping | None", default=None) -> vol.Marker:
    return vol.Required(
        key,
        description={
            "suggested_value": (config and config.get(key, default)) or default
        },
    )


class BaseFlow(ce.ConfigEntryBaseFlow if TYPE_CHECKING else object):
    """Mixin providing commons for Config and Option flows"""

    if TYPE_CHECKING:

        _is_bluetooth: bool
        device_id: str
        device_config: mlc.DeviceConfigType
        device_descriptor: MerossDeviceDescriptor
        device_placeholders: dict[str, str]

        profile_config: mlc.ProfileConfigType
        profile_placeholders: dict[str, str]
        _is_keyerror: bool
        """Set when the async_step_profile is invoked to fix a device key configuration."""
        _profile_entry: ce.ConfigEntry | None
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

        # TODO: implement a 'config' child logger to forward logs instead of using the api

        class BindConfigType(TypedDict):
            ssid: NotRequired[str | None]
            password: NotRequired[str | None]
            timezone: NotRequired[str | None]
            server: NotRequired[str | None]
            check: NotRequired[bool | None]
            key: NotRequired[str | None]
            userid: NotRequired[int | None]

        bind_config: BindConfigType
        bind_placeholders: dict[str, str]

        # instance properties managed with show_form_errorcontext
        # and async_show_form_with_errors
        _config_schema: dict[vol.Marker, Any]
        _errors: dict[str, str] | None

    VERSION = 1
    MINOR_VERSION = 1

    _is_bluetooth = False
    _is_keyerror = False
    _profile_entry = None

    device_placeholders = {
        "device_type": "",
        "device_id": "",
        "host": "",
    }

    profile_placeholders = {
        "email": "",
        "placeholder": "",
    }

    @cached_property
    def api(self):
        return ComponentApi.get(self.hass)

    @cached_property
    def http_client(self):
        """Plain MerossHttpClient. When using ensure the host/key are correctly set/refreshed."""
        return HttpClient(
            "",
            self.api,
            from_=mlc.DOMAIN,
            trigger_src=self.__class__.__name__,
            loop=self.hass.loop,
        )

    async def async_get_device_client(self, device_id: str) -> "MerossClient | None":
        """Returns a suitable low level device client to query/configure the device.
        This instance must not be modified since it could be an active client used by a Device.
        """
        api = self.api
        try:
            device = api.devices[device_id]
            if device:
                if device._bluetooth:
                    return device._bluetooth
                elif device._http_active:
                    return device._http_active
                elif device._mqtt_publish:
                    return MQTTConnection.Client(
                        device_id,
                        device._mqtt_publish,
                        key=self.device_config.get(mlc.CONF_KEY) or "",
                        trigger_src=self.__class__.__name__,
                    )
                else:
                    return None
        except KeyError:
            pass

        if self._is_bluetooth:
            return api.get_bt_device(device_id)

        device_config = self.device_config
        host = device_config.get(mlc.CONF_HOST)
        if host:
            http_client = self.http_client
            http_client.host = host
            http_client.key = device_config.get(mlc.CONF_KEY) or ""
            http_client.descriptor = self.device_descriptor
            return http_client

        profile = api.profiles.get(self.device_descriptor.userId)
        if profile and profile.allow_mqtt_publish:
            mqttconnections = await profile.get_or_create_mqttconnections(device_id)
            if mqttconnections:
                return MQTTConnection.Client(
                    device_id,
                    mqttconnections[0],
                    key=device_config.get(mlc.CONF_KEY) or "",
                    trigger_src=self.__class__.__name__,
                )

        return None

    @contextmanager
    def show_form_errorcontext(self):
        """Context manager to catch and show exceptions errors in the user form.
        The CONF_ERROR key will be added as a string label to the UI schema
        containing the exception message so to provide better (untranslated)
        error context."""

        def _render_exception(e: BaseException):
            self._config_schema = {
                _optional(CONF_ERROR, None, f"{e.__class__.__name__}({str(e)})"): str
            }

        try:
            self._config_schema = {}
            self._errors = None
            yield
        except cloudapi.CloudApiError as e:
            self._errors = {CONF_ERROR: FlowErrorKey.INVALID_AUTH.value}
            _render_exception(e)
        except FlowError as e:
            self._errors = {"base": e.key}
            if e.__cause__:
                _render_exception(e.__cause__)
        except Exception as e:
            self._errors = {CONF_ERROR: FlowErrorKey.CANNOT_CONNECT.value}
            _render_exception(e)

    def async_show_form_with_errors(
        self,
        step_id: str,
        *,
        config_schema: "dict[vol.Marker, Any]" = {},
        description_placeholders: "Mapping[str, str] | None" = None,
    ):
        """modularize errors managment: use together with show_form_errorcontext and get_schema_with_errors"""
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(self._config_schema | config_schema),
            errors=self._errors,
            description_placeholders=description_placeholders,
        )

    def clone_api_diagnostic_config(
        self, config: mlc.DeviceConfigType | mlc.ProfileConfigType
    ):
        """Clone actual ComponentApi diagnostic settings on new device/profile config being created."""
        if api_config := self.api.config:
            if mlc.CONF_LOGGING_LEVEL in api_config:
                config[mlc.CONF_LOGGING_LEVEL] = api_config[mlc.CONF_LOGGING_LEVEL]
            if mlc.CONF_OBFUSCATE in api_config:
                config[mlc.CONF_OBFUSCATE] = api_config[mlc.CONF_OBFUSCATE]

    def finish_flow(
        self,
        config: mlc.DeviceConfigType | mlc.ProfileConfigType | mlc.HubConfigType,
        reload: bool = False,
    ):
        """Used in ConfigFlow/OptionsFlow to terminate and exit (with save)."""
        raise NotImplementedError()

    @staticmethod
    def merge_userinput(
        config: mlc.ManagerConfigType,
        user_input: "Mapping",
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

    async def async_step_profile(self, user_input: "Mapping[str, Any] | None" = None):
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
                    cloudapiclient = cloudapi.CloudApiClient(
                        "",
                        api,
                        session=async_get_clientsession(self.hass),
                        obfuscate_func=api.loggable_any,
                    )
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
                                    _optional(CONF_ERROR, None, str(mfa_error)): str,
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
                    return self.finish_flow(profile_config)

                # abort any eventual duplicate progress flow
                # also, even if the user was creating a new profile,
                # updates any eventually existing one...
                # we will eventually abort this flow later
                unique_id = f"profile.{profile_config[mc.KEY_USERID_]}"
                profile_flow = api.get_config_flow(unique_id)
                if profile_flow and (profile_flow["flow_id"] != self.flow_id):
                    hass.config_entries.flow.async_abort(profile_flow["flow_id"])
                profile_entry = api.get_config_entry(unique_id)
                if profile_entry:
                    hass.config_entries.async_update_entry(
                        profile_entry,
                        data=profile_config,
                    )
                    if not self._is_keyerror:
                        # this flow was creating a profile but it's entry is
                        # already in place (and updated)
                        return self.async_abort(reason=FlowErrorKey.ALREADY_CONFIGURED)
                else:
                    # this profile config is new either because of keyerror
                    # or user creating a cloud profile.
                    self.clone_api_diagnostic_config(profile_config)
                    if self._is_keyerror:
                        # this flow is managing a device but since the profile
                        # entry is new, we'll directly setup that
                        await hass.config_entries.async_add(
                            # there's a bad compatibility issue between core 2024.1 and
                            # previous versions up to latest 2023 on ConfigEntry. Namely:
                            # previous core versions used positional args in ConfigEntry
                            # while core 2024.X moves to full kwargs with required minor_version
                            # this patch is the best I can think of
                            ce.ConfigEntry(
                                version=self.VERSION,
                                minor_version=self.MINOR_VERSION,  # required since 2024.1
                                discovery_keys=MappingProxyType(
                                    {}
                                ),  # required since 2024.10
                                domain=mlc.DOMAIN,
                                title=profile_config[mc.KEY_EMAIL],
                                data=profile_config,
                                options={},  # required since 2024.6
                                source=ce.SOURCE_USER,
                                unique_id=unique_id,
                                subentries_data=(),  # required since 2025.3
                            )
                            if (mlc.hac.MAJOR_VERSION, mlc.hac.MINOR_VERSION) >= (2025, 3)  # type: ignore
                            else (
                                ce.ConfigEntry(  # type: ignore
                                    version=self.VERSION,
                                    minor_version=self.MINOR_VERSION,  # required since 2024.1
                                    discovery_keys=MappingProxyType(
                                        {}
                                    ),  # required since 2024.10
                                    domain=mlc.DOMAIN,
                                    title=profile_config[mc.KEY_EMAIL],
                                    data=profile_config,
                                    options={},  # required since 2024.6
                                    source=ce.SOURCE_USER,
                                    unique_id=unique_id,
                                )
                                if mlc.hac.MAJOR_VERSION >= 2024
                                else ce.ConfigEntry(  # type: ignore
                                    version=self.VERSION,
                                    domain=mlc.DOMAIN,
                                    title=profile_config[mc.KEY_EMAIL],
                                    data=profile_config,
                                    source=ce.SOURCE_USER,
                                    unique_id=unique_id,
                                )
                            )
                        )
                    else:
                        # this ConfigFlow was creating(user) a profile and looks like
                        # no entry exists
                        if await self.async_set_unique_id(unique_id, raise_on_progress=False):  # type: ignore
                            return self.async_abort(
                                reason=FlowErrorKey.ALREADY_CONFIGURED
                            )
                        return self.async_create_entry(
                            title=profile_config[mc.KEY_EMAIL], data=profile_config
                        )

                # this flow is managing a device: assert self._is_keyerror
                self.device_config[mlc.CONF_KEY] = profile_config[mc.KEY_KEY]
                return await self.async_step_device()

        config_schema = self._config_schema

        if self._profile_entry:
            # this is a profile OptionsFlow
            profile = self.api.profiles.get(profile_config[mc.KEY_USERID_])
            require_login = not (profile and profile.token_is_valid)
        else:
            # this is not a profile OptionsFlow so we'd need to login for sure
            # with full credentials
            config_schema[_optional(mlc.CONF_CLOUD_REGION, profile_config)] = (
                selector.SelectSelector(
                    {
                        "options": list(cloudapi.API_URL_MAP.keys()),
                        "translation_key": mlc.CONF_CLOUD_REGION,
                        "mode": selector.SelectSelectorMode.DROPDOWN,
                    }
                )
            )
            config_schema[_required(mlc.CONF_EMAIL, profile_config)] = str
            require_login = True
        if require_login:
            # token expired or not a profile OptionFlow: we'd need to login again
            config_schema[_required(mlc.CONF_PASSWORD, profile_config)] = str
            config_schema[_required(mlc.CONF_SAVE_PASSWORD, profile_config, False)] = (
                bool
            )
            if profile_config.get(mlc.CONF_MFA_CODE):
                # this is when we already have credentials (OptionsFlow then)
                # and those are stating the login was an MFA
                config_schema[vol.Optional(mlc.CONF_MFA_CODE)] = str
        config_schema[_required(mlc.CONF_ALLOW_MQTT_PUBLISH, profile_config, False)] = (
            bool
        )
        config_schema[
            _required(mlc.CONF_CHECK_FIRMWARE_UPDATES, profile_config, False)
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

    async def async_step_bind(self, user_input: "BindConfigType | None" = None):
        hass = self.hass
        api = self.api
        device_id = self.device_id
        device_descriptor = self.device_descriptor
        device_config = self.device_config
        device_ssid = None
        device_server = str(device_descriptor.main_broker)
        device_key = device_config.get(mc.KEY_KEY) or ""
        try:
            bind_config = self.bind_config
        except AttributeError:
            # We'll set some defaults here based off descriptor
            # but, if we're able to connect, we'll use the Appliance.System.Debug data
            # to pre-fill the bind config
            bind_config = self.bind_config = {
                mc.KEY_SERVER: device_server,
                mc.KEY_KEY: device_key,
                mc.KEY_USERID_: (
                    int(device_descriptor.userId)
                    if device_descriptor.userId.isnumeric()
                    else None
                ),
            }

        device_client = await self.async_get_device_client(device_id)

        # Build a list of known valid broker addresses.
        # We start from locally binded active connections by querying loaded devices
        mqtt_connections: dict[str, tuple["MQTTConnection", HostAddress, bool]] = {}
        ha_mqtt_connection = api.mqtt_connection
        for _device in api.active_devices():
            if _device.mqtt_locallyactive:
                _broker = _device.descriptor.main_broker
                mqtt_connections[f"HomeAssistant (mqtt://{_broker})"] = (
                    ha_mqtt_connection,
                    _broker,
                    False,
                )
        # This should work as a fallback but is rather fragile since we don't know for
        # sure the effective address of the HA broker
        if not mqtt_connections and ha_mqtt_connection.mqtt_is_subscribed:
            mqtt_connections[
                f"HomeAssistant (mqtt://{ha_mqtt_connection.broker.host})"
            ] = (ha_mqtt_connection, ha_mqtt_connection.broker, True)
        # Add also Meross cloud bound device connections
        for _profile in api.active_profiles():
            for _broker, _mqtt_connection in _profile.mqttconnections.items():
                mqtt_connections[f"{_profile.display_name} (mqtt://{_broker})"] = (
                    _mqtt_connection,
                    _mqtt_connection.broker,
                    False,
                )

        with self.show_form_errorcontext():

            if not device_client:
                raise FlowError(FlowErrorKey.CANNOT_CONNECT)

            # retrieve the current bind config/state (for safety)
            p_debug: "mt.system.Debug" = await device_client.async_request_ns_payload(
                mn.Appliance_System_Debug
            )
            p_network = p_debug[mc.KEY_NETWORK]
            device_ssid = p_network[mc.KEY_SSID]
            p_cloud = p_debug[mc.KEY_CLOUD]
            device_server = f"{p_cloud[mc.KEY_MAINSERVER]}:{p_cloud[mc.KEY_MAINPORT]}"
            device_userid = p_cloud[mc.KEY_USERID]

            if user_input:

                bind_config[mc.KEY_SSID] = ssid = user_input.get(mc.KEY_SSID)
                bind_config[mc.KEY_PASSWORD] = password = user_input.get(
                    mc.KEY_PASSWORD
                )
                bind_config[mc.KEY_SERVER] = server = user_input.get(mc.KEY_SERVER)
                bind_config["check"] = check = user_input.get("check")
                bind_config[mc.KEY_KEY] = key = user_input.get(mc.KEY_KEY)
                bind_config[mc.KEY_USERID_] = user_id = user_input.get(mc.KEY_USERID_)

                configure_wifi = ssid and (ssid != device_ssid)
                if configure_wifi and not password:
                    raise ValueError("Password is required when SSID is provided")

                configure_mqtt_args = {}
                if server and (server != device_server):
                    # user wants to configure new broker
                    try:
                        # check if an available profile/connection was chosen
                        mqtt_connection, server_address, _resolve_address = (
                            mqtt_connections[server]
                        )
                        # force key,userid if a connection was choosen
                        if key or user_id:
                            api.log(
                                api.WARNING,
                                "Provided 'key' and 'userid' will be ignored when using a predefined connection",
                            )
                        key = mqtt_connection.profile.key
                        user_id = mqtt_connection.profile.userid
                    except KeyError:
                        # or if manual entry
                        _match = re.match(
                            r"mqtt://(?P<host>(?:[a-zA-Z0-9\-\.]+|\d{1,3}(?:\.\d{1,3}){3}|localhost))(?::(?P<port>\d{1,5}))?",
                            server,
                        ) or re.match(
                            r"(?P<host>(?:[a-zA-Z0-9\-\.]+|\d{1,3}(?:\.\d{1,3}){3}|localhost))(?::(?P<port>\d{1,5}))?",
                            server,
                        )
                        if not _match:
                            raise FlowError(FlowErrorKey.BROKER_ADDRESS_INVALID)
                        _port = _match.group("port")
                        server_address = HostAddress.build(
                            _match.group("host"),
                            int(_port) if _port else 8883,
                        )
                        _resolve_address = True
                        key = (
                            key or device_key
                        )  # empty key looks like not supported anymore
                        user_id = (
                            str(device_userid) if user_id is None else str(user_id)
                        )

                    if _resolve_address:
                        # we have to check the broker address is a network bound IPV4 address
                        # since localhost would have no meaning (or a wrong one) in the device
                        import socket

                        # eventually patch the HA mqtt 'default' port
                        # if server_address.port == 1883:
                        #    server_address.port = 8883
                        # getaddrinfo contains the resolved ipv4 address(es) weather or not
                        # our broker_address.host is an ipv6 or ipv4 host name/addr
                        for addrinfo in socket.getaddrinfo(
                            socket.getfqdn(server_address.host),
                            server_address.port,
                            family=socket.AF_INET,
                            type=socket.SOCK_STREAM,
                            proto=socket.IPPROTO_TCP,
                        ):
                            # addrinfo: (family, type, proto, canonname, sockaddr)
                            if addrinfo[4][0] == "127.0.0.1":
                                # localhost could work in our mqtt.Client check but
                                # it will not when configured in the device bind
                                # so we're trying to resolve it to a valid network name
                                from homeassistant.helpers.network import get_url
                                from yarl import URL

                                _host = URL(get_url(hass, allow_ip=True)).host
                                if not _host:
                                    raise FlowError(FlowErrorKey.BROKER_ADDRESS_INVALID)
                                server_address.host = _host
                                break

                    server = str(server_address)
                    if server != device_server:
                        if check:
                            _mqttclient = MQTTDeviceClient(
                                server_address,
                                api,
                                key=key,
                                uuid=device_id,
                                user_id=user_id,
                                loop=hass.loop,
                                sslcontext=get_default_no_verify_ssl_context(),
                            )
                            try:
                                await asyncio.wait_for(
                                    await _mqttclient.async_connect(server_address), 5
                                )
                            except Exception as e:
                                api.log_exception(
                                    api.WARNING,
                                    e,
                                    "MQTT connection check to %s",
                                    server_address,
                                )
                                raise FlowError(
                                    FlowErrorKey.BROKER_CONNECTION_ERROR
                                ) from e
                            finally:
                                await _mqttclient.async_shutdown()

                        configure_mqtt_args["host"] = server_address.host
                        configure_mqtt_args["port"] = server_address.port
                        configure_mqtt_args["key"] = key
                        configure_mqtt_args["userid"] = user_id

                if not configure_mqtt_args:
                    # broker update skipped, check if we want to update just key/userid

                    key = user_input.get(mc.KEY_KEY) or device_key
                    user_id = user_input.get(mc.KEY_USERID_) or device_userid
                    if (key != device_key) or (user_id != device_userid):
                        configure_mqtt_args["key"] = key
                        configure_mqtt_args["userid"] = str(user_id)
                        server = device_server

                if configure_mqtt_args:
                    api.log(
                        api.DEBUG,
                        "Initiating MQTT binding to %s (key=%s, user_id=%s)",
                        api.loggable_broker(server),  # type: ignore
                        api.loggable_any(key),
                        api.loggable_profile_id(user_id),  # type: ignore
                    )
                    response = await device_client.async_configure_mqtt(
                        **configure_mqtt_args
                    )
                    if response.method != mc.METHOD_SETACK:
                        raise Exception("Failed MQTT binding configuration")
                    api.log(api.DEBUG, "MQTT binding to %s was succesfull", api.loggable_broker(server))  # type: ignore
                    device_config[mlc.CONF_KEY] = key  # type: ignore

                if configure_wifi:
                    await device_client.async_configure_wifi(
                        ssid=ssid, password=password  # type: ignore
                    )
                    # TODO: use external step flow to wait for (eventual) MQTT/DHCP
                    # discovery

                return self.finish_flow(device_config)
            else:
                bind_config[mc.KEY_SSID] = (
                    None if device_ssid == "MEROSS_STA" else device_ssid
                )
                bind_config[mc.KEY_SERVER] = device_server
                bind_config[mc.KEY_USERID_] = device_userid

        try:
            ssid_list = await device_client.async_get_ssid_scan()
        except:
            ssid_list = []

        return self.async_show_form_with_errors(
            "bind",
            config_schema={
                _optional(mc.KEY_SSID, bind_config): selector.SelectSelector(
                    {
                        "options": ssid_list,
                        "custom_value": True,
                    }
                ),
                _optional(mc.KEY_PASSWORD, bind_config): str,
                _optional(mc.KEY_SERVER, bind_config): selector.SelectSelector(
                    {
                        "options": list(mqtt_connections),
                        "custom_value": True,
                    }
                ),
                _required("check", bind_config, True): bool,
                _optional(mc.KEY_KEY, bind_config): str,
                _optional(mc.KEY_USERID_, bind_config): cv.positive_int,
            },
            description_placeholders={"ssid": device_ssid, "server": device_server},
        )

    async def _async_http_discovery(
        self, host: str, key: str | None
    ) -> tuple[mlc.DeviceConfigType, MerossDeviceDescriptor]:
        http_client = self.http_client
        http_client.host = host
        http_client.key = key or ""
        descriptor = await http_client.async_identify()
        return (
            {
                mlc.CONF_HOST: host,
                mlc.CONF_PAYLOAD: descriptor.payload,
                mlc.CONF_KEY: http_client.key,
                mlc.CONF_DEVICE_ID: descriptor.uuid,
            },
            descriptor,
        )

    async def _async_mqtt_discovery(
        self, device_id: str, key: str, descriptor: MerossDeviceDescriptor | None
    ) -> tuple[mlc.DeviceConfigType, MerossDeviceDescriptor]:
        mqttconnections: list[MQTTConnection] = []
        if descriptor:
            profile = self.api.profiles.get(descriptor.userId)
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
        for identify_coro in asyncio.as_completed(
            [
                mqttconnection.async_identify_device(device_id, key or "")
                for mqttconnection in mqttconnections
            ]
        ):
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


class ConfigFlow(BaseFlow, ce.ConfigFlow, domain=mlc.DOMAIN):
    """Handle a config flow for Meross IoT local LAN."""

    if TYPE_CHECKING:
        # TODO: DHCP_DISCOVERIES is actually an ever growing dict..we could use it to populate a list
        # of available host when user starts a flow and/or add epoch information to remove stale entries
        # also we should ensure the discoveries are removed when a device is configured.
        DHCP_DISCOVERIES: Final[dict[str, Any]]

    DHCP_DISCOVERIES = {}

    @staticmethod
    def async_get_options_flow(config_entry):
        return OptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        """initial step (menu) for user initiated flows"""
        if profile := next(iter(self.api.active_profiles()), None):
            self.device_config = {mlc.CONF_KEY: profile.key}  # type: ignore[assignment]
        else:
            self.device_config = {mlc.CONF_KEY: self.api.key}  # type: ignore[assignment]
        self.profile_config = {}  # type: ignore[assignment]
        return self.async_show_menu(
            step_id="user",
            menu_options=["profile", "device"],
        )

    async def async_step_hub(self, user_input=None):
        """configure the MQTT discovery device key"""
        if user_input is not None:
            return self.async_create_entry(title="MQTT Hub", data=user_input)
        if await self.async_set_unique_id(mlc.DOMAIN):
            return self.async_abort(reason=FlowErrorKey.ALREADY_CONFIGURED)
        return self.async_show_form(
            step_id="hub",
            data_schema=vol.Schema(
                {
                    _optional(mlc.CONF_KEY, None, mlc.PARAM_DEFAULT_KEY): str,
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
                        *await self._async_http_discovery(
                            user_input[mlc.CONF_HOST], user_input.get(mlc.CONF_KEY)
                        ),
                    )
                except MerossKeyError:
                    return await self.async_step_keyerror()

        return self.async_show_form_with_errors(
            "device",
            config_schema={
                _required(mlc.CONF_HOST, device_config): str,
                _optional(mlc.CONF_KEY, device_config): str,
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
            discovery_info,
            MerossDeviceDescriptor(discovery_info[mlc.CONF_PAYLOAD]),
        )

    async def async_step_bluetooth(self, discovery_info: "BluetoothServiceInfoBleak"):
        api = self.api

        try:
            bt_device = await api.async_bt_advertisement(discovery_info, self)
            uuid = bt_device.uuid
            descriptor = bt_device.descriptor
            config_entry = await self.async_set_unique_id(uuid, raise_on_progress=True)
            device_config: mlc.DeviceConfigType
            if config_entry:
                device_config = dict(config_entry.data)  # type: ignore
                device_config[mlc.CONF_PROTOCOL] = Transport.BLUETOOTH
            else:
                device_config = {
                    mlc.CONF_KEY: "",
                    mlc.CONF_DEVICE_ID: uuid,
                    mlc.CONF_PAYLOAD: descriptor.payload,
                    mlc.CONF_PROTOCOL: Transport.BLUETOOTH,
                }
                self.clone_api_diagnostic_config(device_config)

            # TODO: reconcile with _async_set_device_config
            self._is_bluetooth = True
            self.device_id = uuid
            self.device_config = device_config
            self.device_descriptor = descriptor
            self.device_placeholders = {
                "device_type": descriptor.productnametype,
                "device_id": uuid,
            }
            self._set_flow_title(f"{descriptor.type} - {uuid}")
            self.bind_config = {}
            return await self.async_step_bind()
        except AbortFlow:
            raise
        except Exception as e:
            api.log_exception(api.DEBUG, e, "async_step_bluetooth")
            return self.async_abort(reason=FlowErrorKey.CANNOT_CONNECT)

    async def async_step_dhcp(self, discovery_info: "DhcpServiceInfo"):
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
                            return self.async_abort(
                                reason=FlowErrorKey.ALREADY_CONFIGURED
                            )
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
                        return self.async_abort(reason=FlowErrorKey.ALREADY_CONFIGURED)

                    case _:
                        continue

        except Exception as exception:
            api.log_exception(api.WARNING, exception, "DHCP update check")

        try:
            # try device identification so the user/UI has a good context to start with
            for profile in api.active_profiles():
                try:
                    return await self._async_set_device_config(
                        *await self._async_http_discovery(host, profile.key)
                    )
                except Exception:
                    pass

            if key := api.key:
                try:
                    return await self._async_set_device_config(
                        *await self._async_http_discovery(host, key)
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
        # If our ComponentApi is already running it will manage the discovery itself
        # so this flow is only useful when MerossLan has no configuration yet
        # and we leverage the default mqtt discovery to setup our manager
        mqtt_connection = self.api.mqtt_connection
        if mqtt_connection.mqtt_is_subscribed:
            return self.async_abort(reason=FlowErrorKey.ALREADY_CONFIGURED)
        # try setup the mqtt subscription
        # this call might not register because of errors or because of an overlapping
        # request from 'async_setup_entry' (we're preventing overlapped calls to MQTT
        # subscription)
        if await mqtt_connection.async_mqtt_subscribe():
            # ok, now pass along the discovering mqtt message so our ComponentApi state machine
            # gets to work on this
            await mqtt_connection.async_mqtt_message(discovery_info)
        # just in case, setup the MQTT Hub entry to enable the (default) device key configuration
        # if the entry hub is already configured this will disable the discovery
        # subscription (by returning 'already_configured') stopping any subsequent async_step_mqtt message:
        # our ComponentApi should already be in place
        return await self.async_step_hub()

    async def async_step_finalize(self, user_input=None):
        ConfigFlow.DHCP_DISCOVERIES.pop(self.device_id[-12:].lower(), None)  # type: ignore
        return self.async_create_entry(
            title=self._title,
            data=self.device_config,
        )

    def finish_flow(
        self,
        config: "Mapping[str, Any]",
        reload: bool = False,
    ):
        return self.async_create_entry(title=self._title, data=config)

    async def _async_set_device_config(
        self,
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
        self._abort_if_unique_id_configured(
            updates=device_config, reload_on_update=False  # type: ignore
        )

        self.clone_api_diagnostic_config(device_config)
        self.device_id = uuid
        self.device_config = device_config
        self.device_descriptor = descriptor
        self.device_placeholders = {
            "device_type": descriptor.productnametype,
            "device_id": uuid,
        }
        devname = (
            device_info.get(mc.KEY_DEVNAME) or uuid
            if (profile := self.api.profiles.get(descriptor.userId))
            and (device_info := profile.get_device_info(uuid))
            else uuid
        )
        self._set_flow_title(f"{descriptor.type} - {devname}")
        return self.async_show_form(
            step_id="finalize",
            data_schema=vol.Schema({}),
            description_placeholders=self.device_placeholders,
        )

    def _set_flow_title(self, flow_title: str):
        self._title = flow_title
        self.context["title_placeholders"] = {"name": flow_title}


class OptionsFlow(BaseFlow, ce.OptionsFlow):
    """
    Manage device options configuration
    """

    if TYPE_CHECKING:
        config: mlc.HubConfigType | mlc.DeviceConfigType | mlc.ProfileConfigType
        config_entry: Final[ce.ConfigEntry[ConfigEntryManager]]
        config_entry_id: Final[str]
        repair_issue_id: Final[str | None]

    _MENU_OPTIONS = {
        "hub": ["hub", "diagnostics"],
        "profile": ["profile", "diagnostics"],
        "device": ["device", "diagnostics", "bind", "unbind"],
    }

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
        # WARNING: HA core 2024.12 introduced new properties for config_entry/config_entry_id
        # Right now we're overwriting the implementation hoping for the good...
        self.config_entry = config_entry
        self.config_entry_id = config_entry.entry_id
        self.config = dict(config_entry.data)  # type: ignore
        self.repair_issue_id = repair_issue_id

    async def async_step_init(self, user_input=None):
        match ConfigEntryType.get_type_and_id(self.config_entry.unique_id):
            case (ConfigEntryType.DEVICE, device_id):
                self.device_id = device_id
                device_config: mlc.DeviceConfigType
                self.device_config = device_config = self.config  # type: ignore
                self._is_bluetooth = (
                    device_config.get(mlc.CONF_PROTOCOL) == Transport.BLUETOOTH
                )
                assert device_id == device_config[mlc.CONF_DEVICE_ID]
                try:
                    device: Device = self.config_entry.runtime_data  # type: ignore
                    self.device_descriptor = device.descriptor
                except AttributeError:
                    # if config not loaded the device is None
                    self.device_descriptor = MerossDeviceDescriptor(
                        device_config[mlc.CONF_PAYLOAD]
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

    async def async_step_menu(self, user_input: str):
        if self.repair_issue_id:
            return await getattr(self, f"async_step_{user_input}")(None)
        else:
            return self.async_show_menu(
                step_id="menu",
                menu_options=self._MENU_OPTIONS[user_input],
            )

    async def async_step_hub(self, user_input: "Mapping | None" = None):
        hub_config = self.config
        if user_input is not None:
            self.merge_userinput(hub_config, user_input, mlc.CONF_KEY)
            return self.finish_flow(hub_config)

        config_schema = {
            _optional(mlc.CONF_KEY, hub_config, mlc.PARAM_DEFAULT_KEY): str,
            _required(mlc.CONF_ALLOW_MQTT_PUBLISH, hub_config, True): bool,
        }
        self._setup_entitymanager_schema(config_schema, hub_config)
        return self.async_show_form(
            step_id="hub", data_schema=vol.Schema(config_schema)
        )

    async def async_step_device(self, user_input: "Mapping | None" = None):
        """
        general (common) device configuration allowing key set and
        general parameters to be entered/modified
        """
        api = self.api
        device_id = self.device_id
        device = api.devices[device_id]
        device_config = self.device_config
        device_descriptor = self.device_descriptor
        _is_bluetooth = self._is_bluetooth

        with self.show_form_errorcontext():
            if user_input is not None:
                if _is_bluetooth:
                    self.merge_userinput(device_config, user_input)
                else:
                    self.merge_userinput(
                        device_config, user_input, mlc.CONF_KEY, mlc.CONF_HOST
                    )
                    try:
                        inner_exception = None
                        device_config_update = None
                        descriptor_update = None
                        _host = user_input.get(mlc.CONF_HOST)
                        _key = user_input.get(mlc.CONF_KEY) or ""
                        _conf_protocol = (
                            user_input.get(mlc.CONF_PROTOCOL) or Transport.AUTO
                        )
                        if _conf_protocol != Transport.HTTP:
                            try:
                                (
                                    device_config_update,
                                    descriptor_update,
                                ) = await self._async_mqtt_discovery(
                                    device_id, _key, device_descriptor
                                )
                            except Exception as e:
                                inner_exception = e
                        if _conf_protocol != Transport.MQTT:
                            if _try_host := (_host or device_descriptor.innerIp):
                                try:
                                    (
                                        device_config_update,
                                        descriptor_update,
                                    ) = await self._async_http_discovery(
                                        _try_host, _key
                                    )
                                except Exception as e:
                                    inner_exception = e

                        if not device_config_update or not descriptor_update:
                            raise inner_exception or FlowError(
                                FlowErrorKey.CANNOT_CONNECT
                            )
                        if device_id != device_config_update[mlc.CONF_DEVICE_ID]:
                            raise FlowError(FlowErrorKey.DEVICE_ID_MISMATCH)
                        device_config[mlc.CONF_PAYLOAD] = device_config_update[
                            mlc.CONF_PAYLOAD
                        ]

                        if self.config_entry.state == ce.ConfigEntryState.SETUP_ERROR:
                            try:  # to fix the device registry in case it was corrupted by #341
                                dev_reg = api.device_registry
                                device_identifiers = {(str(mlc.DOMAIN), device_id)}
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
                                        api.loggable_device_id(device_id),
                                        _name_by_user,
                                    )

                            except Exception as error:
                                api.log_exception(
                                    api.WARNING,
                                    error,
                                    "repairing device registry for %s (uuid:%s)",
                                    descriptor_update.productmodel,
                                    api.loggable_device_id(device_id),
                                )
                            return self.finish_flow(device_config, True)

                    except MerossKeyError:
                        return await self.async_step_keyerror()

                # mc.KEY_TIMEZONE is 'volatile' i.e. a device conf not stored in ConfigEntry
                if (
                    (timezone := device_config.pop(mc.KEY_TIMEZONE, None))
                    and device
                    and (timezone != descriptor_update.timezone)
                ):
                    await device.async_config_device_timezone(timezone)

                # cleanup keys which might wrongly have been persisted
                device_config.pop(mlc.CONF_CLOUD_KEY, None)
                device_config.pop(mlc.CONF_TRACE, None)  # totally removed in v5.0
                return self.finish_flow(device_config)

            else:
                _host = device_config.get(mlc.CONF_HOST)
                _key = device_config.get(mlc.CONF_KEY)

        config_schema = self._config_schema
        if _is_bluetooth:
            _bt_device = api.get_bt_device(device_id)
            self.device_placeholders["host"] = (
                f"BTDevice({_bt_device.address})" if _bt_device else "BTDevice(unknown)"
            )
        else:
            self.device_placeholders["host"] = _host or "MQTT"
            config_schema[_optional(mlc.CONF_HOST, None, _host)] = str
            config_schema[_optional(mlc.CONF_KEY, None, _key)] = str
            config_schema[
                _required(mlc.CONF_PROTOCOL, device_config, Transport.AUTO)
            ] = vol.In((Transport.AUTO, Transport.HTTP, Transport.MQTT))
        config_schema[
            _required(
                mlc.CONF_POLLING_PERIOD, device_config, mlc.CONF_POLLING_PERIOD_DEFAULT
            )
        ] = cv.positive_int
        ability = device_descriptor.ability
        if mn.Appliance_Control_Multiple in ability:
            config_schema[
                _optional(mlc.CONF_DISABLE_MULTIPLE, device_config, False)
            ] = bool
        if mn.Appliance_System_Time in ability:
            config_schema[
                _optional(mc.KEY_TIMEZONE, None, device_descriptor.timezone)
            ] = vol.In(await api.async_available_timezones())
        self._setup_entitymanager_schema(config_schema, device_config)
        return self.async_show_form_with_errors(
            "device",
            description_placeholders=self.device_placeholders,
        )

    async def async_step_diagnostics(self, user_input: "Mapping | None" = None):
        # when choosing to start a diagnostic from the OptionsFlow UI we'll
        # reload the entry so we trace also the full initialization process
        # for a more complete insight on the EntityManager context.
        # The info to trigger the trace_open on entry setup is carried through
        # the global ComponentApi.managers_transient_state
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
                state = self.api.managers_transient_state.setdefault(
                    self.config_entry_id, {}
                )
                # we're saving the 'diagnostic state' before reloading so that when tracing starts
                # it'll be dumped at the start of the trace together with config and other info (maybe)
                state[mlc.CONF_TRACE] = (
                    self.config_entry.runtime_data.loggable_diagnostic_state()
                )
                return self.finish_flow(config, True)
            return self.finish_flow(config)

        return self.async_show_form(
            step_id="diagnostics",
            data_schema=vol.Schema(
                {
                    _required(mlc.CONF_CREATE_DIAGNOSTIC_ENTITIES, config, False): bool,
                    vol.Required(
                        mlc.CONF_LOGGING_LEVEL,
                        description={
                            "suggested_value": mlc.CONF_LOGGING_LEVEL_OPTIONS.get(
                                config.get(mlc.CONF_LOGGING_LEVEL, logging.NOTSET),
                                "default",
                            )
                        },
                    ): selector.SelectSelector(
                        {
                            "options": list(mlc.CONF_LOGGING_LEVEL_OPTIONS.values()),
                            "translation_key": mlc.CONF_LOGGING_LEVEL,
                            "mode": selector.SelectSelectorMode.DROPDOWN,
                        }
                    ),
                    _required(mlc.CONF_OBFUSCATE, config, True): bool,
                    _required(mlc.CONF_TRACE, None, False): bool,
                    _optional(
                        mlc.CONF_TRACE_TIMEOUT, config, mlc.CONF_TRACE_TIMEOUT_DEFAULT
                    ): cv.positive_int,
                }
            ),
        )

    async def async_step_unbind(self, user_input=None):
        KEY_ACTION = "post_action"
        KEY_ACTION_DISABLE = "disable"
        KEY_ACTION_DELETE = "delete"

        with self.show_form_errorcontext():
            if user_input:
                api = self.api
                device = api.devices[self.device_id]
                if not (device and device.online):
                    raise FlowError(FlowErrorKey.CANNOT_CONNECT)

                await device.async_unbind()
                action = user_input[KEY_ACTION]
                if action == KEY_ACTION_DISABLE:
                    api.async_create_task(
                        self.hass.config_entries.async_set_disabled_by(
                            self.config_entry_id,
                            ce.ConfigEntryDisabler.USER,
                        ),
                        f".OptionsFlow.async_set_disabled_by",
                        eager_start=False,
                    )
                elif action == KEY_ACTION_DELETE:
                    api.async_create_task(
                        self.hass.config_entries.async_remove(self.config_entry_id),
                        f".OptionsFlow.async_remove",
                        eager_start=False,
                    )
                return self.async_create_entry(data=None)  # type: ignore

        return self.async_show_form_with_errors(
            "unbind",
            config_schema={
                vol.Required(
                    KEY_ACTION,
                    default=KEY_ACTION_DISABLE,  # type: ignore
                ): selector.SelectSelector(
                    {
                        "options": [KEY_ACTION_DISABLE, KEY_ACTION_DELETE],
                        "translation_key": "unbind_post_action",
                    }
                )
            },
        )

    def finish_flow(
        self,
        config: "Mapping[str, Any]",
        reload: bool = False,
    ):
        """Used in OptionsFlow to terminate and exit (with save)."""
        self.hass.config_entries.async_update_entry(self.config_entry, data=config)
        if reload:
            self.api.schedule_entry_reload(self.config_entry_id)
        return self.async_create_entry(data=None)  # type: ignore
