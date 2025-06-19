"""
meross_lan module interface to access Meross Cloud services
"""

import asyncio
from contextlib import asynccontextmanager
from time import time
import typing
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers import storage
from homeassistant.util import dt as dt_util

from . import (
    datetime_from_epoch,
    get_default_ssl_context,
    versiontuple,
)
from .. import const as mlc
from ..const import (
    CONF_CHECK_FIRMWARE_UPDATES,
    CONF_PASSWORD,
    DOMAIN,
)
from ..helpers.obfuscate import OBFUSCATE_DEVICE_ID_MAP, obfuscated_dict
from ..merossclient import MEROSSDEBUG, HostAddress, get_active_broker
from ..merossclient.cloudapi import APISTATUS_TOKEN_ERRORS, CloudApiError
from ..merossclient.mqttclient import MerossMQTTAppClient, generate_app_id
from ..merossclient.protocol import const as mc, namespaces as mn
from .manager import CloudApiClient
from .mqtt_profile import ConnectionSensor, MQTTConnection, MQTTProfile

if TYPE_CHECKING:
    from typing import Final, Unpack

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..const import ProfileConfigType
    from ..devices.hub import HubMixin
    from ..merossclient.cloudapi import (
        DeviceInfoType,
        LatestVersionType,
        SubDeviceInfoType,
    )
    from ..merossclient.protocol.message import MerossMessage
    from .component_api import ComponentApi
    from .device import Device, MerossDeviceDescriptor

    UuidType = str
    DeviceInfoDictType = dict[UuidType, "DeviceInfoType"]


class MerossMQTTConnection(MQTTConnection, MerossMQTTAppClient):

    # here we're acrobatically slottizing MerossMQTTAppClient
    # since it cannot be slotted itself leading to multiple inheritance
    # "forbidden" slots

    if TYPE_CHECKING:
        is_cloud_connection: Final[bool]

    __slots__ = (
        "_asyncio_loop",
        "_future_connected",
        "_tasks",
        "_lock_state",
        "_lock_queue",
        "_rl_dropped",
        "_rl2_queues",
        "_stateext",
        "_subscribe_topics",
        "_unsub_random_disconnect",
    )

    def __init__(self, profile: "MerossProfile", broker: "HostAddress"):
        MerossMQTTAppClient.__init__(
            self,
            profile.key,
            profile.userid,
            app_id=profile.app_id,
            loop=profile.hass.loop,
            sslcontext=get_default_ssl_context(),
        )
        self.is_cloud_connection = True
        MQTTConnection.__init__(self, profile, broker, self.topic_command)
        if profile.isEnabledFor(profile.VERBOSE):
            self.enable_logger(self)  # type: ignore (Loggable is duck-compatible with Logger)

        if MEROSSDEBUG:

            @callback
            async def _async_random_disconnect():
                if self.state_inactive:
                    if MEROSSDEBUG.mqtt_random_connect():
                        self.log(self.DEBUG, "Random connect")
                        await self.async_connect(self.broker)
                else:
                    if MEROSSDEBUG.mqtt_random_disconnect():
                        self.log(self.DEBUG, "Random disconnect")
                        await self.async_disconnect()
                self._unsub_random_disconnect = profile.schedule_async_callback(
                    60, _async_random_disconnect
                )

            self._unsub_random_disconnect = profile.schedule_async_callback(
                60, _async_random_disconnect
            )
        else:
            self._unsub_random_disconnect = None

    # interface: MQTTConnection
    async def async_shutdown(self):
        if self._unsub_random_disconnect:
            self._unsub_random_disconnect.cancel()
            self._unsub_random_disconnect = None
        await MerossMQTTAppClient.async_shutdown(self)
        await MQTTConnection.async_shutdown(self)

    async def entry_update_listener(self, profile: "MerossProfile"):
        await MQTTConnection.entry_update_listener(self, profile)
        if profile.isEnabledFor(profile.VERBOSE):
            self.enable_logger(self)  # type: ignore (Loggable is duck-compatible with Logger)
        else:
            self.disable_logger()

    def get_rl_safe_delay(self, uuid: str):
        return MerossMQTTAppClient.get_rl_safe_delay(self, uuid)

    async def _async_mqtt_publish(
        self,
        device_id: str,
        request: "MerossMessage",
    ):
        return await self.profile.hass.async_add_executor_job(
            self.rl_publish, device_id, request
        )

    @callback
    def _mqtt_connected(self):
        MerossMQTTAppClient._mqtt_connected(self)
        MQTTConnection._mqtt_connected(self)

    @callback
    def _mqtt_published(self):
        if sensor_connection := self.sensor_connection:
            attrs = sensor_connection.extra_state_attributes
            attrs[ConnectionSensor.ATTR_DROPPED] = self.rl_dropped
            attrs[ConnectionSensor.ATTR_PUBLISHED] += 1
            if self.mqtt_is_connected:
                # enforce the state eventually cancelling queued, dropped...
                sensor_connection.native_value = ConnectionSensor.STATE_CONNECTED
            sensor_connection.flush_state()


MerossMQTTConnection.SESSION_HANDLERS = {
    mn.Appliance_System_Online.name: MQTTConnection._handle_Appliance_System_Online,
}


class MerossProfileStoreType(typing.TypedDict):
    appId: str
    # TODO credentials: typing.NotRequired[MerossCloudCredentials]
    deviceInfo: "DeviceInfoDictType"
    deviceInfoTime: float
    latestVersion: list["LatestVersionType"]
    latestVersionTime: float
    token: str | None  # TODO remove
    tokenRequestTime: float


class MerossProfileStore(storage.Store[MerossProfileStoreType]):
    VERSION = 1

    def __init__(self, hass: "HomeAssistant", profile_id: str):
        super().__init__(
            hass,
            MerossProfileStore.VERSION,
            f"{DOMAIN}.profile.{profile_id}",
        )


class MerossProfile(MQTTProfile):
    """
    Represents and manages a cloud account profile used to retrieve keys
    and/or to manage cloud mqtt connection(s)
    """

    if TYPE_CHECKING:
        is_cloud_profile: Final[bool]
        config: ProfileConfigType

        KEY_APP_ID: Final
        KEY_DEVICE_INFO: Final
        KEY_DEVICE_INFO_TIME: Final
        KEY_SUBDEVICE_INFO: Final
        KEY_LATEST_VERSION: Final
        KEY_LATEST_VERSION_TIME: Final
        KEY_TOKEN_REQUEST_TIME: Final

        _data: MerossProfileStoreType
        _unsub_polling_query_device_info: asyncio.TimerHandle | None

    KEY_APP_ID = "appId"
    KEY_DEVICE_INFO = "deviceInfo"
    KEY_DEVICE_INFO_TIME = "deviceInfoTime"
    KEY_SUBDEVICE_INFO = "__subDeviceInfo"
    KEY_LATEST_VERSION = "latestVersion"
    KEY_LATEST_VERSION_TIME = "latestVersionTime"
    KEY_TOKEN_REQUEST_TIME = "tokenRequestTime"

    __slots__ = (
        "apiclient",
        "_data",
        "_store",
        "_unsub_polling_query_device_info",
        "_device_info_time",
    )

    def __init__(
        self, profile_id: str, api: "ComponentApi", config_entry: "ConfigEntry"
    ):
        self.is_cloud_profile = True
        MQTTProfile.__init__(
            self, profile_id, api=api, hass=api.hass, config_entry=config_entry
        )
        # state of the art for credentials is that they're mixed in
        # into the config_entry.data but this is prone to issues and confusing
        # so we 'might' decide to move them to a dict valued key in configentry.data
        # or completely remove and store them in storage. Whatever
        # we might desire compatibility between storage formats with previous versions
        # so we're putting the migration code in 5.0.0 but still not going
        # to change the version(s) in storage/config. At the moment I'm still very confused
        # and opting to keep the credentials where they are embedded in ConfigEntry
        self.apiclient = CloudApiClient(self, self.config)
        self._store = MerossProfileStore(self.hass, profile_id)
        self._unsub_polling_query_device_info = None

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
                data[self.KEY_APP_ID] = generate_app_id()
            if not isinstance(data.get(self.KEY_DEVICE_INFO), dict):
                data[self.KEY_DEVICE_INFO] = {}
            self._device_info_time = data.get(self.KEY_DEVICE_INFO_TIME, 0.0)
            if not isinstance(self._device_info_time, float):
                data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time = 0.0
            if not isinstance(data.get(self.KEY_LATEST_VERSION), list):
                data[self.KEY_LATEST_VERSION] = []
            if self.KEY_LATEST_VERSION_TIME not in data:
                data[self.KEY_LATEST_VERSION_TIME] = 0.0
            if self.KEY_TOKEN_REQUEST_TIME not in data:
                data[self.KEY_TOKEN_REQUEST_TIME] = 0.0

            if not data.get(mc.KEY_TOKEN):
                # the token would be auto-refreshed when needed in
                # _async_token_manager but we'd eventually need
                # to just setup the issue registry in case we're
                # not configured to automatically refresh
                self.apiclient.credentials = None
                await self.async_token_refresh()
        else:
            self._device_info_time = 0.0
            self._data: MerossProfileStoreType = {
                self.KEY_APP_ID: generate_app_id(),
                mc.KEY_TOKEN: self.config.get(mc.KEY_TOKEN),
                self.KEY_DEVICE_INFO: {},
                self.KEY_DEVICE_INFO_TIME: 0.0,
                self.KEY_LATEST_VERSION: [],
                self.KEY_LATEST_VERSION_TIME: 0.0,
                self.KEY_TOKEN_REQUEST_TIME: 0.0,
            }

        if mc.KEY_MQTTDOMAIN in self.config:
            broker = HostAddress.build(self.config[mc.KEY_MQTTDOMAIN])  # type: ignore
            mqttconnection = MerossMQTTConnection(self, broker)
            mqttconnection.schedule_connect(broker)

        # compute the next cloud devlist query and setup the scheduled callback
        next_query_epoch = (
            self._device_info_time + mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT
        )
        next_query_delay = next_query_epoch - time()
        if next_query_delay < mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT:
            # we'll give some breath to the init process
            next_query_delay = mlc.PARAM_CLOUDPROFILE_DELAYED_SETUP_TIMEOUT
        self._unsub_polling_query_device_info = self.schedule_async_callback(
            next_query_delay,
            self._async_query_device_info,
        )

    async def async_shutdown(self):
        if self._unsub_polling_query_device_info:
            self._unsub_polling_query_device_info.cancel()
            self._unsub_polling_query_device_info = None
        await super().async_shutdown()
        self.api.profiles[self.id] = None

    # interface: ConfigEntryManager
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
            if self.need_query_device_info():
                # retrigger the poll at the right time since async_query_devices
                # might be called for whatever reason 'asynchronously'
                # at any time (say the user does a new cloud login or so...)
                if self._unsub_polling_query_device_info:
                    self._unsub_polling_query_device_info.cancel()
                    await self._async_query_device_info()

    def get_logger_name(self) -> str:
        return f"profile_{self.loggable_profile_id(self.id)}"

    def loggable_diagnostic_state(self):
        if self.obfuscate:
            store_data = obfuscated_dict(self._data)
            # the profile contains uuid as keys and obfuscation
            # is not smart enough (but OBFUSCATE_DEVICE_ID_MAP is already
            # filled with uuid(s) from the profile device_info(s) and
            # the device_info(s) were already obfuscated in data)
            store_data[MerossProfile.KEY_DEVICE_INFO] = {
                OBFUSCATE_DEVICE_ID_MAP[device_id]: device_info
                for device_id, device_info in store_data[
                    MerossProfile.KEY_DEVICE_INFO
                ].items()
            }
            return {"store": store_data}
        else:
            return {"store": self._data}

    # interface: ApiProfile
    def attach_mqtt(self, device: "Device"):
        descr = device.descriptor
        try:
            if device.online:
                if device.device_debug:
                    try:
                        broker = get_active_broker(device.device_debug)
                    except Exception:
                        broker = descr.main_broker
                else:
                    broker = descr.main_broker
            else:
                # decide which broker to connect to based off the most recent info
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
                "attach_mqtt for device uuid:%s (%s)",
                self.loggable_device_id(device.id),
                device.name,
            )
            try:
                # fallback if we have the KEY_MQTTDOMAIN
                broker = HostAddress.build(self.config[mc.KEY_MQTTDOMAIN])  # type: ignore
            except:
                return

        mqttconnection = self._get_mqttconnection(broker)
        mqttconnection.attach(device)
        if mqttconnection.state_inactive:
            mqttconnection.schedule_connect(broker)

    # interface: self
    @property
    def app_id(self):
        return self._data[self.KEY_APP_ID]

    @property
    def token_is_valid(self):
        return bool(self._data.get(mc.KEY_TOKEN))

    @property
    def userid(self):
        return self.config[mc.KEY_USERID_]

    def link(self, device: "Device"):
        """
        Device linking to a cloud profile sets the environment for
        the device MQTT attachment/connection. This process uses a lot
        of euristics to ensure the device really belongs to this cloud
        profile.
        A device binded to a cloud profile should:
        - have the same userid
        - have the same key
        - have a broker address compatible with the profile available brokers
        - be present in the device_info db
        The second check could be now enforced since the new Meross signin api
        tells us ('mqttDomain') which is the (only) broker assigned to this profile.
        It was historically not this way since devices binded to a cloud account could
        be spread among a pool of brokers.
        Presence in the device_info db might be unreliable since the query is only
        done once in 24 hours and thus, the db being out of sync
        """
        super().link(device)
        if device_info := self._data[self.KEY_DEVICE_INFO].get(device.id):
            device.update_device_info(device_info)
        if latest_version := self.get_latest_version(device.descriptor):
            device.update_latest_version(latest_version)

    def get_device_info(self, uuid: str):
        return self._data[self.KEY_DEVICE_INFO].get(uuid)

    def get_latest_version(self, descriptor: "MerossDeviceDescriptor"):
        """returns LatestVersionType info if device has an update available"""
        _type = descriptor.type
        _version = versiontuple(descriptor.firmwareVersion)
        # the LatestVersionType struct reports also the subType for the firmware
        # but the meaning of this field is a bit confusing since a lot of traces
        # are reporting the value "un" (undefined?) for the vast majority.
        # Also, the mcu field (should contain a list of supported mcus?) is not
        # reported in my api queries and I don't have enough data to guess anything
        # at any rate, actual implementation is not proceeding with effective
        # update so these infos we gather and show are just cosmethic right now and
        # will not harm anyone ;)
        # _subtype = descriptor.subType
        for latest_version in self._data[self.KEY_LATEST_VERSION]:
            if (
                latest_version.get(mc.KEY_TYPE)
                == _type
                # and latest_version.get(mc.KEY_SUBTYPE) == _subtype
            ):
                if versiontuple(latest_version.get(mc.KEY_VERSION, "")) > _version:
                    return latest_version
                else:
                    return None
        return None

    def need_query_device_info(self):
        return (
            time() - self._device_info_time
        ) > mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT

    async def async_check_query_latest_version(self, epoch: float):
        if (
            self.config.get(CONF_CHECK_FIRMWARE_UPDATES)
            and (epoch - self._data[self.KEY_LATEST_VERSION_TIME])
            > mlc.PARAM_CLOUDPROFILE_QUERY_LATESTVERSION_TIMEOUT
        ):
            self._data[self.KEY_LATEST_VERSION_TIME] = epoch
            async with self._async_credentials_manager(
                "async_check_query_latest_version"
            ) as credentials:
                if not credentials:
                    return
                self._data[self.KEY_LATEST_VERSION] = (
                    await self.apiclient.async_device_latestversion()
                )
                self._schedule_save_store()
                for device in self.api.active_devices():
                    if latest_version := self.get_latest_version(device.descriptor):
                        device.update_latest_version(latest_version)

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
                if mqttconnection.broker == broker:
                    return
            mqttconnection = await self._async_get_mqttconnection(broker)
            if mqttconnection:
                mqttconnections.append(mqttconnection)

        await _add_connection(self.config.get(mc.KEY_MQTTDOMAIN))

        if device_info := self.get_device_info(device_id):
            await _add_connection(device_info.get(mc.KEY_DOMAIN))
            await _add_connection(device_info.get(mc.KEY_RESERVEDDOMAIN))

        return mqttconnections

    def _get_mqttconnection(self, broker: HostAddress) -> MerossMQTTConnection:
        """
        Returns an existing connection from the managed pool or create one and add
        to the mqttconnections pool. The connection state is not ensured.
        """
        connection_id = str(broker)
        if connection_id in self.mqttconnections:
            return self.mqttconnections[connection_id]  # type: ignore
        return MerossMQTTConnection(self, broker)

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
            await asyncio.wait_for(await mqttconnection.async_connect(broker), 5)
            return mqttconnection
        except Exception as exception:
            self.log_exception(
                self.DEBUG, exception, "waiting to subscribe to %s", str(broker)
            )
            return None

    async def async_token_refresh(self):
        """
        Called when the stored token is dropped (expired) or when needed.
        Tries silently (re)login or raises an issue.
        """
        try:
            data = self._data
            if (_time := time()) < data[
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
                config[CONF_PASSWORD], config
            )
            # set our (stored) key so the ConfigEntry update will find everything in place
            # and not trigger any side effects. No need to re-trigger _schedule_save_store
            # since it should still be pending...
            data[mc.KEY_TOKEN] = credentials[mc.KEY_TOKEN]
            self.log(self.INFO, "Meross api token was automatically refreshed")
            profile_entry = self.api.get_config_entry(f"profile.{self.id}")
            if profile_entry:
                # weird enough if this isnt true...
                profile_config = dict(profile_entry.data)
                profile_config.update(credentials)
                # watchout: this will in turn call self.entry_update_listener
                self.hass.config_entries.async_update_entry(
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
                await self.async_token_refresh()
            )
            if not credentials:
                self.log(self.WARNING, f"{msg} cancelled: missing cloudapi token")
            yield credentials
        except CloudApiError as clouderror:
            self.log_exception(self.WARNING, clouderror, msg)
            if clouderror.apistatus in APISTATUS_TOKEN_ERRORS:
                self.apiclient.credentials = None
                if self._data.pop(mc.KEY_TOKEN, None):  # type: ignore
                    await self.async_token_refresh()
        except Exception as exception:
            self.log_exception(self.WARNING, exception, msg)

    async def _async_query_device_info(self):
        self._unsub_polling_query_device_info = self.schedule_async_callback(
            mlc.PARAM_CLOUDPROFILE_QUERY_DEVICELIST_TIMEOUT,
            self._async_query_device_info,
        )
        async with self._async_credentials_manager(
            "_async_query_device_info"
        ) as credentials:
            if not credentials:
                return
            self.log(
                self.DEBUG,
                "Querying device list - last query was at: %s",
                datetime_from_epoch(
                    self._device_info_time, dt_util.DEFAULT_TIME_ZONE
                ).isoformat(),
            )
            self._device_info_time = time()
            device_info_new = await self.apiclient.async_device_devlist()
            await self._process_device_info_new(device_info_new)
            self._data[self.KEY_DEVICE_INFO_TIME] = self._device_info_time
            self._schedule_save_store()
            # this is a 'low relevance task' as a new feature (in 4.3.0) to just provide hints
            # when new updates are available: we're not going (yet) to manage the
            # effective update since we're not able to do any basic validation
            # of the whole process and it might be a bit 'dangerous'
            await self.async_check_query_latest_version(self._device_info_time)

    async def _async_query_subdevices(self, device_id: str):
        async with self._async_credentials_manager(
            "_async_query_subdevices"
        ) as credentials:
            if not credentials:
                return None
            self.log(
                self.DEBUG,
                "Querying hub subdevice list (uuid:%s)",
                self.loggable_device_id(device_id),
            )
            return await self.apiclient.async_hub_getsubdevices(device_id)
        return None

    async def _process_device_info_new(
        self, device_info_list_new: list["DeviceInfoType"]
    ):
        api_devices = self.api.devices
        device_info_dict = self._data[self.KEY_DEVICE_INFO]
        device_info_removed = {device_id for device_id in device_info_dict.keys()}
        device_info_unknown: list["DeviceInfoType"] = []
        for device_info in device_info_list_new:
            with self.exception_warning("_process_device_info_new"):
                device_id = device_info[mc.KEY_UUID]
                # preserved (old) dict of hub subdevices to process/carry over
                # for Hub(s)
                sub_device_info_dict: dict[str, "SubDeviceInfoType"] | None
                if device_id in device_info_dict:
                    # already known device
                    device_info_removed.remove(device_id)
                    sub_device_info_dict = device_info_dict[device_id].get(
                        self.KEY_SUBDEVICE_INFO
                    )
                else:
                    # new device
                    sub_device_info_dict = None
                device_info_dict[device_id] = device_info

                try:
                    device = api_devices[device_id]
                except KeyError:
                    device_info_unknown.append(device_info)
                    continue
                if not device:  # device loaded
                    continue
                if device.get_type() is mlc.DeviceType.HUB:
                    if sub_device_info_dict is None:
                        sub_device_info_dict = {}
                    device_info[self.KEY_SUBDEVICE_INFO] = sub_device_info_dict
                    sub_device_info_list_new = await self._async_query_subdevices(
                        device_id
                    )
                    if sub_device_info_list_new is not None:
                        await self._process_subdevice_info_new(
                            typing.cast("HubMixin", device),
                            sub_device_info_dict,
                            sub_device_info_list_new,
                        )
                device.update_device_info(device_info)

        for device_id in device_info_removed:
            self.log(
                self.DEBUG,
                "The uuid:%s has been removed from the cloud profile",
                self.loggable_device_id(device_id),
            )
            device_info_dict.pop(device_id)
            if device := self.linkeddevices.get(device_id):
                self.unlink(device)

        if len(device_info_unknown):
            await self._process_device_info_unknown(device_info_unknown)

    async def _process_subdevice_info_new(
        self,
        hub_device: "HubMixin",
        sub_device_info_dict: dict[str, "SubDeviceInfoType"],
        sub_device_info_list_new: list["SubDeviceInfoType"],
    ):
        sub_device_info_removed = {
            subdeviceid for subdeviceid in sub_device_info_dict.keys()
        }
        sub_device_info_unknown: list["SubDeviceInfoType"] = []

        for sub_device_info in sub_device_info_list_new:
            with self.exception_warning("_process_subdevice_info_new"):
                subdeviceid = sub_device_info[mc.KEY_SUBDEVICEID]
                if subdeviceid in sub_device_info_dict:
                    # already known device
                    sub_device_info_removed.remove(subdeviceid)

                sub_device_info_dict[subdeviceid] = sub_device_info
                if subdevice := hub_device.subdevices.get(subdeviceid):
                    subdevice.update_sub_device_info(sub_device_info)
                else:
                    sub_device_info_unknown.append(sub_device_info)

        for subdeviceid in sub_device_info_removed:
            sub_device_info_dict.pop(subdeviceid)
            # TODO: warn the user? should we remove the subdevice from the hub?

        if len(sub_device_info_unknown):
            # subdevices were added.. discovery should be managed by the hub itself
            # TODO: warn the user ?
            pass

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
                    self.loggable_device_id(device_id),
                )
                if self.api.get_config_flow(device_id):
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
