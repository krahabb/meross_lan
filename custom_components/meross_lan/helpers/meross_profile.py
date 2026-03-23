"""
meross_lan module interface to access Meross Cloud services
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, override

from homeassistant.helpers import storage
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

# import core modules instead of symbols to ease patching in a single place
from . import (
    get_default_ssl_context,
    mqtt_profile as mlq,
)
from .. import const as mlc
from ..merossclient import HostAddress, cloudapi, datetime_from_epoch, get_active_broker
from ..merossclient.client.mqtt import MQTTAppClient
from ..merossclient.obfuscate import OBFUSCATE_DICT, OBFUSCATE_UUID_MAP
from ..merossclient.protocol import const as mc

if TYPE_CHECKING:
    from typing import Final, Literal, NotRequired, TypedDict, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..const import ProfileConfigType
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


class MerossMQTTConnection(MQTTAppClient, mlq.MQTTConnection):

    __slots__ = MQTTAppClient._calc_slots()

    def __init__(self, broker: "HostAddress", profile: "MerossProfile"):
        super().__init__(
            broker,
            profile,
            app_id=profile.app_id,
            user_id=profile.userid,
            sslcontext=get_default_ssl_context(),
            loop=profile.loop,
        )


class MerossProfileStore(storage.Store["MerossProfileStoreType"]):
    VERSION = 1

    def __init__(self, hass: "HomeAssistant", profile_id: str):
        super().__init__(
            hass,
            MerossProfileStore.VERSION,
            f"{mlc.DOMAIN}.profile.{profile_id}",
        )

    async def async_remove_and_logout(self, credentials: "MerossCloudCredentials"):

        await super().async_remove()
        await cloudapi.CloudApiClient(
            credentials["userid"],
            credentials=credentials,
            session=async_get_clientsession(self.hass),
        ).async_logout_safe()


class MerossProfile(mlq.MQTTProfile):
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
        config: ProfileConfigType
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
        mlq.MQTTProfile.__init__(self, id, api, config_entry)
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

    async def async_init(self):
        """
        Performs 'cold' initialization of the profile by checking
        if we need to update the device_info and eventually start the
        unknown devices discovery.
        We'll eventually setup the mqtt listeners in case our
        configured devices don't match the profile list. This usually means
        the user has binded a new device and we need to 'discover' it.
        """
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
            mqttconnection = MerossMQTTConnection(
                HostAddress.build(self.config[mc.KEY_MQTTDOMAIN]), self
            )
            try:
                await mqttconnection.async_connect()
            except Exception:
                pass
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

    async def async_shutdown(self):
        await super().async_shutdown()
        await self.apiclient.async_shutdown()
        del self.apiclient
        self.parent.profiles[self.id] = None

    # interface: ConfigEntryManager
    @override
    async def entry_update_listener(self, hass, config_entry: "ConfigEntry"):
        config: ProfileConfigType = config_entry.data  # type: ignore
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
            await super().entry_update_listener(hass, config_entry)
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
    def get_latest_version(self, type: str, subtype: str, /):
        """returns LatestVersionType info if device has an update available"""
        try:
            return (
                self._data[self.KEY_LATEST_VERSION_HISTORY][f"{type}:{subtype}"][-1]
                .values()
                .__iter__()
                .__next__()
            )
        except KeyError:
            return None

    @override
    def get_latest_versions(self, /):
        return self._data[self.KEY_LATEST_VERSION_HISTORY]

    @override
    def get_connection(self, device: "Device"):
        try:
            if device.is_connected:
                if device.device_debug:
                    try:
                        broker = get_active_broker(device.device_debug)
                    except Exception:
                        broker = device.descriptor.main_broker
                else:
                    broker = device.descriptor.main_broker
            else:
                # decide which broker to connect to based off the most recent info
                descr = device.descriptor
                device_info = self._data[self.KEY_DEVICE_INFO][device.id]
                timestamp_fw = descr.time.get(mc.KEY_TIMESTAMP, 0)
                timestamp_di = self._data[self.KEY_DEVICE_INFO_TIME]
                if timestamp_fw > timestamp_di:
                    broker = descr.main_broker
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
        if mqttconnection.state_inactive:
            mqttconnection.create_task(
                mqttconnection.async_connect(),
                "attach_mqtt.schedule_connect",
                eager_start=True,
            )
        return mqttconnection

    @property
    @override
    def is_cloud_profile(self) -> bool:
        return True

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
        mqttconnections: list[mlq.MQTTConnection] = []

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
        if mqttconnection.state_active:
            if mqttconnection.stateext is mqttconnection.STATE_CONNECTED:
                return mqttconnection
            else:
                return None
        try:
            await mqttconnection.async_connect()
            return mqttconnection
        except:
            return None

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
        device_info_removed = {device_id for device_id in device_info_dict.keys()}
        device_info_unknown: list["DeviceInfoType"] = []
        for device_info in device_info_list_new:
            with self.exception_warning("_process_device_info_new"):
                device_id = device_info[mc.KEY_UUID]
                if device_id in device_info_dict:
                    # already known device
                    device_info_removed.remove(device_id)
                device_info_dict[device_id] = device_info

                try:
                    device = api_devices[device_id]
                except KeyError:
                    device_info_unknown.append(device_info)
                    continue
                if not device:  # device unloaded
                    continue
                if device.descriptor.is_hub:
                    async with self._async_credentials_manager(
                        "_async_query_subdevices"
                    ) as credentials:
                        self.log(
                            self.DEBUG,
                            "Querying hub subdevice list (uuid:%s)",
                            uuid=device_id,
                        )
                        device_info[self.KEY_SUBDEVICE_INFO] = (
                            await self.apiclient.async_hub_getsubdevices(device_id)
                        )

                device.update_device_info(device_info, self)

        for device_id in device_info_removed:
            self.log(
                self.DEBUG,
                "The uuid:%s has been removed from the cloud profile",
                uuid=device_id,
            )
            device_info_dict.pop(device_id)
            try:
                self.linkeddevices.pop(device_id).profile_unlinked()
            except KeyError as ke:
                if ke.args[0] != device_id:
                    raise

        if len(device_info_unknown):
            await self._process_device_info_unknown(device_info_unknown)

    async def _process_device_info_unknown(
        self, device_info_unknown: list["DeviceInfoType"]
    ):
        if not self.allow_mqtt_publish:
            self.log(
                self.WARNING,
                "Meross cloud api reported new devices but MQTT publishing is disabled: skipping automatic discovery",
                timeout=604800,  # 1 week
            )
            return

        for device_info in device_info_unknown:
            with self.exception_warning("_process_device_info_unknown"):
                device_id = device_info[mc.KEY_UUID]
                self.log(
                    self.DEBUG,
                    "Trying/Initiating discovery for (new) uuid:%s",
                    uuid=device_id,
                )
                if self.parent.get_config_flow(device_id):
                    continue  # device configuration already progressing
                # cloud conf has a new device
                if domain := device_info.get(mc.KEY_DOMAIN):
                    # try first broker in the cloud configuration
                    if mqttconnection := await self._async_get_mqttconnection(
                        HostAddress.build(domain)
                    ):
                        if await mqttconnection.async_try_discovery(device_id):
                            continue  # identification succeded, a flow has been created
                if (reserveddomain := device_info.get(mc.KEY_RESERVEDDOMAIN)) and (
                    reserveddomain != domain
                ):
                    # try the second broker in the cloud configuration
                    # only if it's different than the previous
                    if mqttconnection := await self._async_get_mqttconnection(
                        HostAddress.build(reserveddomain)
                    ):
                        if await mqttconnection.async_try_discovery(device_id):
                            continue  # identification succeded, a flow has been created

    def _schedule_save_store(self):
        def _data_func():
            return self._data

        self._store.async_delay_save(
            _data_func, mlc.PARAM_CLOUDPROFILE_DELAYED_SAVE_TIMEOUT
        )
