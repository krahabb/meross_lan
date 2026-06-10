"""
meross_lan module interface to access Meross Cloud services
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, override

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

# import core modules instead of symbols to ease patching in a single place
from . import (
    get_default_ssl_context,
)
from .. import const as mlc
from ..merossclient import HostAddress, cloudapi, datetime_from_epoch, versiontuple
from ..merossclient.client.mqtt import MQTTAppClient
from ..merossclient.obfuscate import OBFUSCATE_DICT, OBFUSCATE_UUID_MAP
from ..merossclient.protocol import const as mc
from .mqtt_profile import MQTTConnection, MQTTProfile

if TYPE_CHECKING:
    from typing import Final, Literal, NotRequired, TypedDict, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..merossclient import Descriptor
    from ..merossclient.cloudapi import (
        DeviceInfoType,
        LatestVersionType,
        MerossCloudCredentials,
        SubDeviceInfoType,
    )
    from ..merossclient.protocol.message import MerossMessage
    from .component_api import ComponentApi
    from .device import Device

    class DeviceInfoExtType(DeviceInfoType):
        __subDeviceInfo: NotRequired[list[SubDeviceInfoType]]

    type DeviceInfoStorageType = dict[str, DeviceInfoExtType]
    type LatestVersionStorageType = list[LatestVersionType]
    type LatestVersionHistoryStorageType = dict[str, list[dict[str, LatestVersionType]]]
    """
    {
        "type:subtype": [
            {
                "recording date isoformat": {...LatestVersionType data...}
            },
        ],
    }
    """

    class MerossProfileStoreType(TypedDict):
        appId: str
        # TODO credentials: NotRequired[MerossCloudCredentials]
        deviceInfo: DeviceInfoStorageType
        deviceInfoTime: float
        latestVersion: LatestVersionStorageType
        latestVersionHistory: LatestVersionHistoryStorageType
        # REMOVED: latestVersionTime: float
        token: str | None  # TODO remove
        tokenRequestTime: float


class MerossMQTTConnection(MQTTConnection, MQTTAppClient):

    __slots__ = MQTTAppClient._calc_slots()

    def __init__(self, broker: "HostAddress", profile: "MerossProfile"):
        MQTTConnection.__init__(
            self,
            broker,
            profile,
            app_id=profile.app_id,  # type: ignore
            user_id=profile.userid,  # type: ignore
            sslcontext=get_default_ssl_context(),  # type: ignore
        )


class MerossProfileStore(Store["MerossProfileStoreType"]):
    VERSION = 1

    def __init__(self, hass: "HomeAssistant", profile_id: str):
        Store.__init__(
            self,
            hass,
            MerossProfileStore.VERSION,
            f"{mlc.DOMAIN}.profile.{profile_id}",
        )

    async def async_remove_and_logout(self, credentials: "MerossCloudCredentials"):

        await Store.async_remove(self)
        await cloudapi.CloudApiClient(
            credentials["userid"],
            credentials=credentials,
            session=async_get_clientsession(self.hass),
        ).async_logout_safe()


class MerossProfile(MQTTProfile):
    """
    Represents and manages a cloud account profile used to retrieve keys
    and/or to manage cloud mqtt connection(s).
    TODO: add a Button to manually trigger api device list refresh
    """

    if TYPE_CHECKING:

        KEY_APP_ID: Final
        KEY_DEVICE_INFO: Final
        KEY_DEVICE_INFO_TIME: Final
        KEY_SUBDEVICE_INFO: Final
        KEY_LATEST_VERSION: Final
        KEY_LATEST_VERSION_HISTORY: Final
        KEY_TOKEN_REQUEST_TIME: Final

        _data: MerossProfileStoreType

        # Overrides
        config: Final[mlc.ProfileConfigType]  # type: ignore[override]
        mqttconnections: Final[dict[str, MerossMQTTConnection]]  # type: ignore[override]

    KEY_APP_ID = "appId"
    KEY_DEVICE_INFO = "deviceInfo"
    KEY_DEVICE_INFO_TIME = "deviceInfoTime"
    KEY_SUBDEVICE_INFO = "__subDeviceInfo"
    KEY_LATEST_VERSION = "latestVersion"
    KEY_LATEST_VERSION_HISTORY = "latestVersionHistory"
    # REMOVED KEY_LATEST_VERSION_TIME = "latestVersionTime"
    KEY_TOKEN_REQUEST_TIME = "tokenRequestTime"

    __slots__ = (
        "apiclient",
        "_data",
        "_store",
        "_device_info_time",
    )

    def __init__(self, id: str, api: "ComponentApi", config_entry: "ConfigEntry", /):
        MQTTProfile.__init__(self, id, api, config_entry)
        # state of the art for credentials is that they're mixed in
        # into the config_entry.data but this is prone to issues and confusing
        # so we 'might' decide to move them to a dict valued key in configentry.data
        # or completely remove and store them in storage. Whatever
        # we might desire compatibility between storage formats with previous versions
        # so we're putting the migration code in 5.0.0 but still not going
        # to change the version(s) in storage/config. At the moment I'm still very confused
        # and opting to keep the credentials where they are embedded in ConfigEntry
        self.apiclient = cloudapi.CloudApiClient(
            id, self, credentials=self.config, session=async_get_clientsession(api.hass)
        )
        self._store = MerossProfileStore(api.hass, id)

    async def async_shutdown(self):
        await super().async_shutdown()
        await self.apiclient.async_shutdown()
        del self.apiclient
        self.parent.profiles[self.id] = None

    # interface: ConfigEntryManager
    @override
    async def async_setup_entry(
        self, hass: "HomeAssistant", config_entry: "ConfigEntry", /
    ):
        if data := await self._store.async_load():
            self._data = data
            if self.KEY_APP_ID not in data:
                data[self.KEY_APP_ID] = MQTTAppClient.generate_app_id()
            if type(data.get(self.KEY_DEVICE_INFO)) is not dict:
                data[self.KEY_DEVICE_INFO] = {}
            self._device_info_time = data.get(self.KEY_DEVICE_INFO_TIME, 0.0)
            if type(self._device_info_time) is not float:
                data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time = 0.0
            if type(data.get(self.KEY_LATEST_VERSION)) is not list:
                data[self.KEY_LATEST_VERSION] = []
            if type(data.get(self.KEY_LATEST_VERSION_HISTORY)) is not dict:
                _time = dt_util.utcnow().isoformat()
                data[self.KEY_LATEST_VERSION_HISTORY] = {
                    f"{latest_version.get(mc.KEY_TYPE)}:{latest_version.get(mc.KEY_SUBTYPE)}": [
                        {_time: latest_version}
                    ]
                    for latest_version in data[self.KEY_LATEST_VERSION]
                }
            data.pop("latestVersionTime", None)  # removed key cleanup
            if self.KEY_TOKEN_REQUEST_TIME not in data:
                data[self.KEY_TOKEN_REQUEST_TIME] = 0.0

            if not data.get(mc.KEY_TOKEN):
                # the token would be auto-refreshed when needed in
                # _async_token_manager but we'd eventually need
                # to just setup the issue registry in case we're
                # not configured to automatically refresh
                self.apiclient.credentials = None
                await self._async_token_refresh()
        else:
            self._device_info_time = 0.0
            self._data = {
                self.KEY_APP_ID: MQTTAppClient.generate_app_id(),
                mc.KEY_TOKEN: self.config.get(mc.KEY_TOKEN),
                self.KEY_DEVICE_INFO: {},
                self.KEY_DEVICE_INFO_TIME: 0.0,
                self.KEY_LATEST_VERSION: [],
                self.KEY_LATEST_VERSION_HISTORY: {},
                self.KEY_TOKEN_REQUEST_TIME: 0.0,
            }

        if mc.KEY_MQTTDOMAIN in self.config:
            MerossMQTTConnection(
                HostAddress.build(self.config[mc.KEY_MQTTDOMAIN]), self
            ).start()

        # compute the next cloud devlist query and setup the scheduled callback
        next_query_epoch = (
            self._device_info_time + mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT
        )
        next_query_delay = next_query_epoch - self.time()
        if next_query_delay < mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT:
            # we'll give some breath to the init process
            next_query_delay = mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT
        self.schedule_async_callback(
            next_query_delay,
            self._async_query_device_info,
        )

        await MQTTProfile.async_setup_entry(self, hass, config_entry)

    @override
    async def entry_update_listener(self, hass, config_entry: "ConfigEntry"):
        config: mlc.ProfileConfigType = config_entry.data  # type: ignore
        self.remove_issue(mlc.ISSUE_CLOUD_TOKEN_EXPIRED)
        curr_credentials = self.apiclient.credentials
        if not curr_credentials or (
            curr_credentials[mc.KEY_TOKEN] != config[mc.KEY_TOKEN]
        ):
            with self.exception_warning("updating CloudApiClient credentials"):
                self.log(self.DEBUG, "Updating credentials with new token")
                if curr_credentials:
                    await self.apiclient.async_logout_safe()
                self.apiclient.credentials = config
                self._data[mc.KEY_TOKEN] = config[mc.KEY_TOKEN]
                await self._store.async_save(self._data)

        if self.config.get(mc.KEY_MQTTDOMAIN) != config.get(mc.KEY_MQTTDOMAIN):
            self.schedule_reload()
        else:
            await MQTTProfile.entry_update_listener(self, hass, config_entry)
            # the 'async_check_query_devices' will only occur if we didn't refresh
            # on our polling schedule for whatever reason (invalid token -
            # no connection - whatsoever) so, having a fresh token and likely
            # good connectivity we're going to retrigger that
            if self._need_query_device_info():
                await self._async_query_device_info()

    @override
    def get_logger_name(self) -> str:
        return f"profile_{self.loggable_profile_id(self.id)}"

    @override
    def loggable_diagnostic_state(self):
        if self.obfuscate:
            store_data = OBFUSCATE_DICT(self._data)
            # the profile contains uuid as keys and obfuscation
            # is not smart enough (but OBFUSCATE_DEVICE_ID_MAP is already
            # filled with uuid(s) from the profile device_info(s) and
            # the device_info(s) were already obfuscated in data)
            store_data[MerossProfile.KEY_DEVICE_INFO] = {
                OBFUSCATE_UUID_MAP[device_id]: device_info
                for device_id, device_info in store_data[
                    MerossProfile.KEY_DEVICE_INFO
                ].items()
            }
            return {"store": store_data}
        else:
            return {"store": self._data}

    @override
    def get_device_info(self, uuid: str, /):
        return self._data[self.KEY_DEVICE_INFO].get(uuid)

    @override
    def get_latest_version(self, descriptor: "Descriptor", /):
        """returns LatestVersionType info if device has an update available"""
        try:
            latest_version_history = self._data[self.KEY_LATEST_VERSION_HISTORY][
                f"{descriptor.type}:{descriptor.subType}"
            ]
        except KeyError:
            return None
        latest_versions = [
            latest_version
            for latest_version_entry in latest_version_history
            for latest_version in latest_version_entry.values()
        ]
        if descriptor.fw_version and descriptor.hw_version:
            try:
                _firmware_version = versiontuple(descriptor.fw_version)
                _hardware_version = versiontuple(descriptor.hw_version)
                if _firmware_version[0] == _hardware_version[0]:
                    # Devices on matching hw/fw major versions should stay on that train.
                    same_train_latest_versions = [
                        latest_version
                        for latest_version in latest_versions
                        if versiontuple(latest_version[mc.KEY_VERSION])[0]
                        == _firmware_version[0]
                    ]
                    return (
                        same_train_latest_versions[-1]
                        if same_train_latest_versions
                        else None
                    )
            except (IndexError, KeyError, TypeError, ValueError):
                pass
        try:
            return latest_versions[-1]
        except IndexError:
            return None

    @override
    def get_latest_versions(self, /):
        return self._data[self.KEY_LATEST_VERSION_HISTORY]

    @override
    def link(self, device: "Device"):
        MQTTProfile.link(self, device)
        try:
            device.update_device_info(self._data[self.KEY_DEVICE_INFO][device.id], self)
        except KeyError as ke:
            if ke.args[0] != device.id:
                raise
            # missing device info, this is mostly due to a recently added device
            # appearing on MQTT before we had a chance to refresh the device list from the cloud.
            # We eventually post-pone the query if it was recently issued
            self.schedule_async_callback(
                (
                    0
                    if self._device_info_time
                    < (
                        self.time()
                        - mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_RETRIGGER_TIMEOUT
                    )
                    else mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_RETRIGGER_TIMEOUT
                ),
                self._async_query_device_info,
            )

    @override
    def get_connection(self, device: "Device"):
        try:
            if device.is_connected:
                broker = device.descriptor.server
            else:
                # decide which broker to connect to based off the most recent info
                descr = device.descriptor
                device_info = self._data[self.KEY_DEVICE_INFO][device.id]
                timestamp_fw = descr.time.get(mc.KEY_TIMESTAMP, 0)
                timestamp_di = self._data[self.KEY_DEVICE_INFO_TIME]
                if timestamp_fw > timestamp_di:
                    broker = descr.server
                else:
                    if domain := device_info.get(mc.KEY_DOMAIN):
                        broker = HostAddress.build(domain)
                    elif reserveddomain := device_info.get(mc.KEY_RESERVEDDOMAIN):
                        broker = HostAddress.build(reserveddomain)
                    else:
                        raise Exception(
                            "Unable to detect MQTT broker from current cloud device info"
                        )

        except Exception as exception:
            self.log_exception(
                self.WARNING,
                exception,
                "attach_mqtt for %s (uuid:%s)",
                device.display_name,
                uuid=device.id,
            )
            try:
                # fallback if we have the KEY_MQTTDOMAIN
                broker = HostAddress.build(self.config[mc.KEY_MQTTDOMAIN])  # type: ignore
            except:
                return

        mqttconnection = self._get_mqttconnection(broker)
        mqttconnection.start()  # ensure connection loop is on
        return mqttconnection

    @property
    @override
    def is_cloud_profile(self):
        return True

    @property
    @override
    def rl_rate(self):
        # Ideally we would like to allow customization of the rate limit to allow more customization
        # but this might prove dangerous unless people really know where they're going
        return MQTTConnection.init_rl_rate

    @property
    @override
    def userid(self):
        return self.config[mc.KEY_USERID_]

    # interface: self
    @property
    def app_id(self):
        return self._data[self.KEY_APP_ID]

    @property
    def token_is_valid(self):
        return bool(self._data.get(mc.KEY_TOKEN))

    async def get_or_create_mqttconnections(self, device_id: str):
        """
        Returns a list of (active) broker connections according to the cloud configuration.
        The list is empty if device not configured or if the connection(s) to the brokers
        cannot be established (like broker is down any network issue)
        """
        mqttconnections: list[MQTTConnection] = []

        async def _add_connection(domain: str | None):
            if not domain:
                return
            broker = HostAddress.build(domain)
            for mqttconnection in mqttconnections:
                if mqttconnection.id == broker:
                    return
            mqttconnection = await self._async_get_mqttconnection(broker)
            if mqttconnection:
                mqttconnections.append(mqttconnection)

        await _add_connection(self.config.get(mc.KEY_MQTTDOMAIN))

        if device_info := self.get_device_info(device_id):
            await _add_connection(device_info.get(mc.KEY_DOMAIN))
            await _add_connection(device_info.get(mc.KEY_RESERVEDDOMAIN))

        return mqttconnections

    def _get_mqttconnection(self, broker: HostAddress):
        """
        Returns an existing connection from the managed pool or create one and add
        to the mqttconnections pool. The connection state is not ensured.
        """
        try:
            return self.mqttconnections[str(broker)]
        except KeyError:
            return MerossMQTTConnection(broker, self)

    async def _async_get_mqttconnection(self, broker: HostAddress):
        """
        Retrieve a connection for the broker from the managed pool (or creates it)
        and tries ensuring it is connected returning None if not (this is especially
        needed when we want to setup a broker connection for device identification
        and we so need it soon).
        """
        mqttconnection = self._get_mqttconnection(broker)
        if not mqttconnection.is_connected:
            try:
                await mqttconnection.async_connect()
            except:
                return None
        return mqttconnection

    async def _async_token_refresh(self):
        """
        Called when the stored token is dropped (expired) or when needed.
        Tries silently (re)login or raises an issue.
        """
        try:
            data = self._data
            if (_time := self.time()) < data[
                self.KEY_TOKEN_REQUEST_TIME
            ] + mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT:
                return None
            data[self.KEY_TOKEN_REQUEST_TIME] = _time
            self._schedule_save_store()
            config = self.config
            if mlc.CONF_PASSWORD not in config:
                raise Exception("Missing profile password")
            if config.get(mlc.CONF_MFA_CODE):
                raise Exception("MFA required")
            credentials = await self.apiclient.async_token_refresh(
                config[mlc.CONF_PASSWORD], config
            )
            # set our (stored) key so the ConfigEntry update will find everything in place
            # and not trigger any side effects. No need to re-trigger _schedule_save_store
            # since it should still be pending...
            data[mc.KEY_TOKEN] = credentials[mc.KEY_TOKEN]
            self.log(self.INFO, "Meross api token was automatically refreshed")
            profile_entry = self.parent.get_config_entry(f"profile.{self.id}")
            if profile_entry:
                # weird enough if this isnt true...
                profile_config = dict(profile_entry.data)
                profile_config.update(credentials)
                # watchout: this will in turn call self.entry_update_listener
                self.parent.config_entries.async_update_entry(
                    profile_entry,
                    data=profile_config,
                )
            return credentials
        except Exception as exception:
            self.log_exception(self.WARNING, exception, "Meross api token auto-refresh")
            self.create_issue(
                mlc.ISSUE_CLOUD_TOKEN_EXPIRED,
                severity=self.IssueSeverity.WARNING,
                translation_placeholders={"email": config.get(mc.KEY_EMAIL)},
            )
            return None

    @asynccontextmanager
    async def _async_credentials_manager(self, msg: str, *args, **kwargs):
        try:
            # this is called every time we'd need a token to query the cloudapi
            # it just yields the current one or tries it's best to recover a fresh
            # token with a guard to avoid issuing too many requests...
            credentials = self.apiclient.credentials or (
                await self._async_token_refresh()
            )
            if credentials:
                yield credentials
            else:
                self.log(self.WARNING, f"{msg} cancelled: missing cloudapi token")
        except cloudapi.CloudApiError as clouderror:
            self.log_exception(self.WARNING, clouderror, msg)
            if clouderror.apistatus in cloudapi.APISTATUS_TOKEN_ERRORS:
                self.apiclient.credentials = None
                if self._data.pop(mc.KEY_TOKEN, None):  # type: ignore
                    await self._async_token_refresh()
        except Exception as exception:
            self.log_exception(self.WARNING, exception, msg)

    def _need_query_device_info(self):
        return (
            self.time() - self._device_info_time
        ) > mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT

    async def _async_query_device_info(self):
        self.schedule_async_callback(
            mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
            self._async_query_device_info,
        )
        async with self._async_credentials_manager(
            "_async_query_device_info"
        ) as credentials:
            self.log(
                self.DEBUG,
                "Querying device info - last query was at: %s",
                datetime_from_epoch(self._device_info_time, dt_util.DEFAULT_TIME_ZONE),
            )
            self._device_info_time = self.time()
            if self.config.get(mlc.CONF_CHECK_FIRMWARE_UPDATES):
                with self.exception_warning("_async_query_device_info - latestversion"):
                    self._data[self.KEY_LATEST_VERSION] = (
                        await self.apiclient.async_device_latestversion()
                    )
                    latest_version_history = self._data[self.KEY_LATEST_VERSION_HISTORY]
                    _time = datetime_from_epoch(
                        self._device_info_time, dt_util.UTC
                    ).isoformat()
                    for latest_version in self._data[self.KEY_LATEST_VERSION]:
                        _key = f"{latest_version.get(mc.KEY_TYPE)}:{latest_version.get(mc.KEY_SUBTYPE)}"
                        try:
                            _list = latest_version_history[_key]
                            for _latest in _list[-1].values():
                                if _latest != latest_version:
                                    _list.append({_time: latest_version})
                        except KeyError:
                            latest_version_history[_key] = [{_time: latest_version}]

            await self._process_device_info_new(
                await self.apiclient.async_device_devlist()  # type: ignore
            )
            self._data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time
            self._schedule_save_store()

    async def _process_device_info_new(
        self, device_info_list_new: list["DeviceInfoExtType"]
    ):
        api_devices = self.parent.devices
        device_info_dict = self._data[self.KEY_DEVICE_INFO]
        device_info_removed = {device_id for device_id in device_info_dict}
        for device_info in device_info_list_new:
            with self.exception_warning("_process_device_info_new"):
                uuid = device_info[mc.KEY_UUID]
                if uuid in device_info_dict:
                    # already known device
                    device_info_removed.remove(uuid)
                device_info_dict[uuid] = device_info
                device_name = device_info.get(mc.KEY_DEVNAME, "")
                device_type = device_info.get(mc.KEY_DEVICETYPE, "")
                if device_type.startswith(mc.TYPE_HUB):
                    async with self._async_credentials_manager(
                        "_async_query_subdevices"
                    ) as credentials:
                        self.log(
                            self.DEBUG,
                            "Querying '%s' subdevice list (uuid:%s)",
                            device_name,
                            uuid=uuid,
                        )
                        device_info[MerossProfile.KEY_SUBDEVICE_INFO] = (
                            await self.apiclient.async_hub_getsubdevices(uuid)
                        )
                try:
                    device = api_devices[uuid]
                except KeyError:
                    # unknown/unconfigured device
                    self.log(
                        self.DEBUG,
                        "Meross cloud api reported new device '%s' (type:%s, uuid:%s): initiating discovery",
                        device_name,
                        device_type,
                        uuid=uuid,
                    )
                    if domain := device_info.get(mc.KEY_DOMAIN):
                        # try first broker in the cloud configuration
                        if mqttconnection := await self._async_get_mqttconnection(
                            HostAddress.build(domain)
                        ):
                            if await mqttconnection.async_try_discovery(uuid, self.key):
                                continue  # identification succeded, a flow has been created
                    if (reserveddomain := device_info.get(mc.KEY_RESERVEDDOMAIN)) and (
                        reserveddomain != domain
                    ):
                        # try the second broker in the cloud configuration
                        # only if it's different from the previous
                        if mqttconnection := await self._async_get_mqttconnection(
                            HostAddress.build(reserveddomain)
                        ):
                            if await mqttconnection.async_try_discovery(uuid, self.key):
                                continue  # identification succeded, a flow has been created
                    continue

                if device:
                    device.update_device_info(device_info, self)

        for uuid in device_info_removed:
            device_info = device_info_dict.pop(uuid)
            self.log(
                self.DEBUG,
                "Device '%s' (type:%s, uuid:%s) has been removed from the cloud profile",
                device_info.get(mc.KEY_DEVNAME, mc.TYPE_UNKNOWN),
                device_info.get(mc.KEY_DEVICETYPE, mc.TYPE_UNKNOWN),
                uuid=uuid,
            )
            try:
                self.unlink(self.linkeddevices[uuid])
            except KeyError as ke:
                if ke.args[0] != uuid:
                    raise

    def _schedule_save_store(self):
        def _data_func():
            return self._data

        self._store.async_delay_save(
            _data_func, mlc.PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT
        )
