""""""
from asyncio import Future, run_coroutine_threadsafe
import base64
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import re
from typing import Any, Callable, Coroutine
from unittest.mock import MagicMock, Mock, patch

import aiohttp
from freezegun.api import FrozenDateTimeFactory, StepTickTimeFactory, freeze_time
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import MerossApi, MerossDevice, const as mlc
from custom_components.meross_lan.meross_profile import MerossMQTTConnection
from custom_components.meross_lan.merossclient import cloudapi, const as mc
from emulator import MerossEmulator, build_emulator as emulator_build_emulator

from . import const as tc


class ConfigEntryMocker(contextlib.AbstractAsyncContextManager):
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MockConfigEntry,
        auto_add: bool = True,
        auto_setup: bool = True,
    ) -> None:
        super().__init__()
        self.hass = hass
        self.config_entry = config_entry
        self.auto_setup = auto_setup
        if auto_add:
            config_entry.add_to_hass(hass)

    @property
    def state(self):
        return self.config_entry.state

    async def async_setup(self):
        result = await self.hass.config_entries.async_setup(self.config_entry.entry_id)
        await self.hass.async_block_till_done()
        return result

    async def async_unload(self):
        return await self.hass.config_entries.async_unload(self.config_entry.entry_id)

    async def __aenter__(self):
        if self.auto_setup:
            assert await self.async_setup()
        return self

    async def __aexit__(self, __exc_type, __exc_value, __traceback):
        if self.config_entry.state.recoverable:
            assert await self.async_unload()
        return await super().__aexit__(__exc_type, __exc_value, __traceback)


class MQTTHubEntryMocker(ConfigEntryMocker):
    def __init__(
        self,
        hass: HomeAssistant,
        auto_add: bool = True,
        auto_setup: bool = True,
    ):
        super().__init__(
            hass,
            MockConfigEntry(
                domain=mlc.DOMAIN,
                data=tc.MOCK_HUB_CONFIG,
                unique_id=mlc.DOMAIN,
            ),
            auto_add=auto_add,
            auto_setup=auto_setup,
        )


class ProfileEntryMocker(ConfigEntryMocker):
    def __init__(
        self,
        hass: HomeAssistant,
        auto_add: bool = True,
        auto_setup: bool = True,
    ):
        super().__init__(
            hass,
            MockConfigEntry(
                domain=mlc.DOMAIN,
                data=tc.MOCK_PROFILE_CONFIG,
                unique_id=f"profile.{tc.MOCK_PROFILE_CONFIG[mc.KEY_USERID_]}",
            ),
            auto_add=auto_add,
            auto_setup=auto_setup,
        )


def build_emulator(model: str) -> MerossEmulator:
    # Watchout: this call will not use the uuid and key set
    # in the filename, just DEFAULT_UUID and DEFAULT_KEY
    return emulator_build_emulator(
        tc.EMULATOR_TRACES_PATH + tc.EMULATOR_TRACES_MAP[model],
        tc.MOCK_DEVICE_UUID,
        tc.MOCK_KEY,
    )


def build_emulator_for_profile(
    profile_id: str,
    *,
    model: str | None = None,
    device_id=tc.MOCK_DEVICE_UUID,
    key=tc.MOCK_KEY,
) -> MerossEmulator:
    """
    This call will setup the emulator patching its configuration
    in order to be 'binded' to the provided cloud profile data.
    Specifying a 'model' will try to match a suitable deviceType
    in the stored profile else it will just default
    """
    domain = None
    reservedDomain = None

    storekey = f"{mlc.DOMAIN}.profile.{profile_id}"
    cloudprofiledata = tc.MOCK_PROFILE_STORAGE.get(storekey)
    if cloudprofiledata:
        cloudprofiledata = cloudprofiledata["data"]
        assert cloudprofiledata[mc.KEY_USERID_] == profile_id
        assert (key := cloudprofiledata[mc.KEY_KEY])
        device_info_dict = cloudprofiledata.get("deviceInfo")
        if device_info_dict:
            for device_info in cloudprofiledata["deviceInfo"].values():
                device_type = device_info[mc.KEY_DEVICETYPE]  # type: ignore
                if (model is not None) and (model != device_type):
                    # we asked for a specific model
                    continue
                model = device_type
                device_id = device_info[mc.KEY_UUID]  # type: ignore
                domain = device_info.get(mc.KEY_DOMAIN)
                reservedDomain = device_info.get(mc.KEY_RESERVEDDOMAIN)
                break
            else:
                # no matching device found in profile
                pass

    if model is None:
        # no error if we can't match a device in the profile
        # just provide a default
        model = mc.TYPE_MSS310
    emulator = emulator_build_emulator(
        tc.EMULATOR_TRACES_PATH + tc.EMULATOR_TRACES_MAP[model],
        device_id,
        key,
    )

    fw = emulator.descriptor.firmware
    fw[mc.KEY_USERID] = int(profile_id)

    if domain:
        host, port = cloudapi.parse_domain(domain)
        fw[mc.KEY_SERVER] = host
        fw[mc.KEY_PORT] = port

    if reservedDomain:
        if domain == reservedDomain:
            fw.pop(mc.KEY_SECONDSERVER, None)
            fw.pop(mc.KEY_SECONDPORT, None)
        else:
            host, port = cloudapi.parse_domain(reservedDomain)
            fw[mc.KEY_SECONDSERVER] = host
            fw[mc.KEY_SECONDPORT] = port

    return emulator


def build_emulator_config_entry(
    emulator: MerossEmulator, config_data: dict | None = None
):
    """
    Builds a consistent config_entry for an emulated device with HTTP communication.
    Use config_data to override/add the defaults
    """
    if config_data:
        if mlc.CONF_KEY in config_data:
            emulator.key = config_data[mlc.CONF_KEY]

    data = {
        mlc.CONF_DEVICE_ID: emulator.descriptor.uuid,
        mlc.CONF_HOST: str(id(emulator)),
        mlc.CONF_KEY: emulator.key,
        mlc.CONF_PAYLOAD: {
            mc.KEY_ALL: deepcopy(emulator.descriptor.all),
            mc.KEY_ABILITY: deepcopy(emulator.descriptor.ability),
        },
        mlc.CONF_PROTOCOL: mlc.CONF_PROTOCOL_HTTP,
        mlc.CONF_POLLING_PERIOD: tc.MOCK_POLLING_PERIOD,
    }

    if config_data:
        data.update(config_data)

    return MockConfigEntry(
        domain=mlc.DOMAIN,
        data=data,
        unique_id=data[mlc.CONF_DEVICE_ID],
        version=1,
    )


@contextlib.contextmanager
def emulator_mock(
    emulator_: MerossEmulator | str,
    aioclient_mock: AiohttpClientMocker,
    frozen_time: FrozenDateTimeFactory | StepTickTimeFactory | None = None,
):
    """
    This context provides an emulator working on HTTP by leveraging
    the aioclient_mock.
    This is a basic mock which is not polluting HA
    """
    try:
        if isinstance(emulator_, str):
            emulator_ = build_emulator(emulator_)
        emulator_.host = str(id(emulator_))

        async def _handle_http_request(method, url, data):
            response = emulator_.handle(data)  # pylint: disable=no-member
            if frozen_time is not None:
                frozen_time.tick(
                    timedelta(seconds=tc.MOCK_HTTP_RESPONSE_DELAY)
                )  # emulate http roundtrip time
            return AiohttpClientMockResponse(method, url, json=response)

        # we'll use the uuid so we can mock multiple at the same time
        # and the aioclient_mock will route accordingly
        aioclient_mock.post(
            f"http://{emulator_.host}/config",
            side_effect=_handle_http_request,
        )

        yield emulator_

    finally:
        # remove the mock from aioclient
        aioclient_mock.clear_requests()


class DeviceContext:
    hass: HomeAssistant
    time: FrozenDateTimeFactory | StepTickTimeFactory
    emulator: MerossEmulator
    config_entry: MockConfigEntry
    device_id: str
    api: MerossApi | None
    device: MerossDevice | None

    _config_entry_loaded: bool = False
    _warp_task: Future | None = None
    _warp_run: bool

    async def perform_coldstart(self):
        """
        to be called after setting up a device (context) to actually
        execute the cold-start polling sequence.
        After this the device should be online and all the polling
        namespaces done
        """
        if self._config_entry_loaded is False:
            await self.async_load_config_entry()
        assert self.device
        self.time.tick(timedelta(seconds=mlc.PARAM_COLDSTARTPOLL_DELAY))
        async_fire_time_changed_exact(self.hass)
        await self.hass.async_block_till_done()
        assert self.device.online

    async def async_load_config_entry(self):
        assert self.device is None
        assert self._config_entry_loaded is False
        hass = self.hass
        self._config_entry_loaded = await hass.config_entries.async_setup(
            self.config_entry.entry_id
        )
        assert self._config_entry_loaded
        await hass.async_block_till_done()
        if self.api is None:
            self.api = hass.data[mlc.DOMAIN]
            assert isinstance(self.api, MerossApi)
        else:
            assert self.api == hass.data[mlc.DOMAIN]
        self.device = self.api.devices[self.device_id]
        assert self.device and not self.device.online

    async def async_unload_config_entry(self):
        """
        Robust finalizer, asserts the config_entry will be correctly unloaded
        and the device cleanup done, whatever the config_entry.state
        """
        hass = self.hass
        assert await hass.config_entries.async_unload(self.config_entry.entry_id)
        self._config_entry_loaded = False
        await hass.async_block_till_done()
        if self.device_id in MerossApi.devices:
            self.device = MerossApi.devices[self.device_id]
        assert self.device is None

    async def async_enable_entity(self, entity_id):
        # entity enable will reload the config_entry
        # by firing a trigger event which will the be collected by
        # config_entries
        # so we have to recover the right instances
        ent_reg = entity_registry.async_get(self.hass)
        ent_reg.async_update_entity(entity_id, disabled_by=None)
        # fire the entity registry changed
        await self.hass.async_block_till_done()
        # perform the reload task after RELOAD_AFTER_UPDATE_DELAY
        await self.async_tick(
            timedelta(seconds=config_entries.RELOAD_AFTER_UPDATE_DELAY)
        )
        # gather the new instances
        self.api = self.hass.data[mlc.DOMAIN]
        self.device = self.api.devices[self.device_id]
        # online the device
        await self.perform_coldstart()

    async def async_tick(self, tick: timedelta):
        print(f"async_tick: time={self.time} tick={tick}")
        self.time.tick(tick)
        async_fire_time_changed_exact(self.hass)
        await self.hass.async_block_till_done()

    async def async_move_to(self, target_datetime: datetime):
        self.time.move_to(target_datetime)
        async_fire_time_changed_exact(self.hass)
        await self.hass.async_block_till_done()

    async def async_warp(
        self,
        timeout: float | int | timedelta | datetime,
        tick: float | int | timedelta = 1,
    ):
        if not isinstance(timeout, datetime):
            if isinstance(timeout, timedelta):
                timeout = self.time() + timeout
            else:
                timeout = self.time() + timedelta(seconds=timeout)
        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        while self.time() < timeout:
            await self.async_tick(tick)

    def warp(self, tick: float | int | timedelta = 0.5):
        """
        starts an asynchronous task which manipulates our
        freze_time so the time passes and get advanced to
        time.time() + timeout.
        While passing it tries to perform HA events rollout
        every tick seconds
        """
        assert self._warp_task is None

        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        def _warp():
            count = 0
            while self._warp_run:
                run_coroutine_threadsafe(self.async_tick(tick), self.hass.loop)
                print(f"Executor: _warp count={count}")
                count += 1

        self._warp_run = True
        self._warp_task = self.hass.async_add_executor_job(_warp)

    async def async_stopwarp(self):
        assert self._warp_task
        self._warp_run = False
        await self._warp_task
        self._warp_task = None


@contextlib.asynccontextmanager
async def devicecontext(
    emulator: MerossEmulator | str,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    time_to_freeze=None,
    config_data: dict | None = None,
):
    """
    This is a 'full featured' context providing an emulator and setting it
    up as a configured device in HA
    It also provides timefreezing
    """
    with freeze_time(time_to_freeze) as frozen_time:
        with emulator_mock(emulator, aioclient_mock, frozen_time) as emulator:
            context = DeviceContext()
            context.hass = hass
            context.time = frozen_time
            context.emulator = emulator
            context.device_id = emulator.descriptor.uuid
            context.api = None
            context.device = None
            context.config_entry = build_emulator_config_entry(
                emulator, config_data=config_data
            )
            context.config_entry.add_to_hass(hass)
            try:
                yield context
            finally:
                await context.async_unload_config_entry()


class CloudApiMocker(contextlib.AbstractContextManager):
    """
    Emulates the Meross server side api by leveraging aioclient_mock
    """

    def __init__(self, aioclient_mock: AiohttpClientMocker, online: bool = True):
        self.aioclient_mock = aioclient_mock
        self._token = None
        self._online = online
        self.api_calls: dict[str, int] = {}
        aioclient_mock.post(
            re.compile(r"https://iot\.meross\.com"),
            side_effect=self._async_handle,
        )

    @property
    def online(self):
        return self._online

    @online.setter
    def online(self, value: bool):
        self._online = value

    def __exit__(self, exc_type, exc_value, traceback):
        self.aioclient_mock.clear_requests()
        return super().__exit__(exc_type, exc_value, traceback)

    @staticmethod
    def _validate_request_payload(data) -> dict:
        if not isinstance(data, dict):
            data = json.loads(data)
        assert mc.KEY_TIMESTAMP in data
        timestamp: int = data[mc.KEY_TIMESTAMP]
        assert mc.KEY_NONCE in data
        nonce: str = data[mc.KEY_NONCE]
        assert mc.KEY_PARAMS in data
        params: str = data[mc.KEY_PARAMS]
        assert mc.KEY_SIGN in data
        sign: str = data[mc.KEY_SIGN]
        assert (
            sign
            == hashlib.md5(
                (cloudapi.SECRET + str(timestamp) + nonce + params).encode("utf-8")
            ).hexdigest()
        )
        params = base64.b64decode(params.encode("utf-8")).decode("utf-8")
        return json.loads(params)

    async def _async_handle(self, method, url, data):
        path: str = url.path
        self.api_calls[path] = self.api_calls.get(path, 0) + 1
        if self._online:
            try:
                result = getattr(self, path.replace("/", "_").lower())(
                    self._validate_request_payload(data)
                )
                return AiohttpClientMockResponse(method, url, json=result)
            except Exception:
                return AiohttpClientMockResponse(
                    method, url, exc=aiohttp.ServerConnectionError()
                )

        return AiohttpClientMockResponse(
            method, url, exc=aiohttp.ServerConnectionError()
        )

    def _v1_auth_login(self, request: dict):
        response = {}
        if mc.KEY_EMAIL not in request:
            response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_INVALID_EMAIL
        elif request[mc.KEY_EMAIL] != tc.MOCK_PROFILE_EMAIL:
            response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_UNEXISTING_ACCOUNT
        elif mc.KEY_PASSWORD not in request:
            response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_MISSING_PASSWORD
        elif request[mc.KEY_PASSWORD] != tc.MOCK_PROFILE_PASSWORD:
            response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_WRONG_CREDENTIALS
        else:
            response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_NO_ERROR
            response[mc.KEY_DATA] = {
                mc.KEY_USERID_: tc.MOCK_PROFILE_ID,
                mc.KEY_EMAIL: tc.MOCK_PROFILE_EMAIL,
                mc.KEY_KEY: tc.MOCK_PROFILE_KEY,
                mc.KEY_TOKEN: tc.MOCK_PROFILE_TOKEN,
            }
            self._token = tc.MOCK_PROFILE_TOKEN
        return response

    def _v1_device_devlist(self, request: dict):
        assert len(request) == 0
        return {
            mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR,
            mc.KEY_DATA: tc.MOCK_PROFILE_CLOUDAPI_DEVLIST,
        }

    def _v1_hub_getsubdevices(self, request: dict):
        response = {}
        if mc.KEY_UUID not in request:
            response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_GENERIC_ERROR
            response[mc.KEY_INFO] = "Missing uuid in request"
        else:
            uuid = request[mc.KEY_UUID]
            if uuid not in tc.MOCK_PROFILE_CLOUDAPI_SUBDEVICE_DICT:
                response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_GENERIC_ERROR
                response[mc.KEY_INFO] = "uuid not registered"
            else:
                response[mc.KEY_APISTATUS] = cloudapi.APISTATUS_NO_ERROR
                response[mc.KEY_DATA] = tc.MOCK_PROFILE_CLOUDAPI_SUBDEVICE_DICT[uuid]
        return response

    def _v1_profile_logout(self, request: dict):
        assert len(request) == 0
        self._token = None
        return {mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR, mc.KEY_DATA: {}}


MqttMockPahoClient = MagicMock
"""MagicMock for `paho.mqtt.client.Client`"""
MqttMockHAClient = MagicMock
"""MagicMock for `homeassistant.components.mqtt.MQTT`."""
MqttMockHAClientGenerator = Callable[..., Coroutine[Any, Any, MqttMockHAClient]]


class HAMQTTMocker(contextlib.AbstractAsyncContextManager):
    mqtt_async_publish: Mock

    def __init__(self):
        self.mqtt_async_publish_patcher = patch(
            "homeassistant.components.mqtt.async_publish"
        )

    async def __aenter__(self):
        """Return `self` upon entering the runtime context."""
        self.mqtt_async_publish = self.mqtt_async_publish_patcher.start()
        self.mqtt_async_publish.side_effect = self._async_publish
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.mqtt_async_publish.side_effect = None
        self.mqtt_async_publish_patcher.stop()
        return await super().__aexit__(exc_type, exc_value, traceback)

    def _async_publish(self, hass, topic: str, payload: str, *args, **kwargs):
        pass


class MerossMQTTMocker(contextlib.AbstractContextManager):
    safe_connect_mock: Mock
    safe_disconnect_mock: Mock

    def __init__(self):
        def _safe_connect(_self: MerossMQTTConnection, host, port):
            _self._stateext = _self.STATE_CONNECTED
            _self._mqtt_connected()

        self.safe_connect_patcher = patch.object(
            MerossMQTTConnection,
            "safe_connect",
            autospec=True,
            side_effect=_safe_connect,
        )
        self.safe_connect_mock = None  # type: ignore

        def _safe_disconnect(_self: MerossMQTTConnection):
            _self._stateext = _self.STATE_DISCONNECTED
            _self._mqtt_disconnected()

        self.safe_disconnect_patcher = patch.object(
            MerossMQTTConnection,
            "safe_disconnect",
            autospec=True,
            side_effect=_safe_disconnect,
        )
        self.safe_disconnect_mock = None  # type: ignore

    def __enter__(self):
        self.safe_connect_mock = self.safe_connect_patcher.start()
        self.safe_disconnect_mock = self.safe_disconnect_patcher.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.safe_connect_mock is not None:
            self.safe_connect_patcher.stop()
            self.safe_connect_mock = None  # type: ignore
        if self.safe_disconnect_mock is not None:
            self.safe_disconnect_patcher.stop()
            self.safe_disconnect_mock = None  # type: ignore
        return super().__exit__(exc_type, exc_value, traceback)
