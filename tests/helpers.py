from asyncio import Future, run_coroutine_threadsafe
import base64
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import re
import time
from typing import Any, Callable, Coroutine, Final
from unittest.mock import ANY, MagicMock, Mock, patch

import aiohttp
from freezegun.api import FrozenDateTimeFactory, StepTickTimeFactory, freeze_time
from homeassistant import config_entries, const as hac
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowManager, FlowResult, FlowResultType
from homeassistant.helpers import entity_registry
from pytest_homeassistant_custom_component.common import MockConfigEntry  # type: ignore
from pytest_homeassistant_custom_component.common import async_fire_time_changed_exact
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import MerossApi, MerossDevice, const as mlc
from custom_components.meross_lan.config_flow import ConfigFlow
from custom_components.meross_lan.diagnostics import async_get_config_entry_diagnostics
from custom_components.meross_lan.helpers import Loggable
from custom_components.meross_lan.meross_profile import (
    MerossCloudProfileStoreType,
    MerossMQTTConnection,
)
from custom_components.meross_lan.merossclient import (
    HostAddress,
    cloudapi,
    const as mc,
    json_loads,
)
from emulator import MerossEmulator, build_emulator as emulator_build_emulator

from . import const as tc


class MockConfigEntry(MockConfigEntry):
    """
    compatibility layer for changing MockConfigEntry signatures between
    HA core 2023.latest and 2024.1
    """

    def __init__(
        self,
        *,
        domain: str,
        data,
        version: int,
        minor_version: int,
        unique_id: str,
    ):
        kwargs = {
            "domain": domain,
            "data": data,
            "version": version,
            "unique_id": unique_id,
        }
        if hac.MAJOR_VERSION >= 2024:
            kwargs["minor_version"] = minor_version
        super().__init__(**kwargs)


async def async_assert_flow_menu_to_step(
    flow: FlowManager,
    result: FlowResult,
    menu_step_id: str,
    next_step_id: str,
    next_step_type: FlowResultType = FlowResultType.FORM,
) -> FlowResult:
    """
    Checks we've entered the menu 'menu_step_id' and chooses 'next_step_id' asserting it works
    Returns the FlowResult at the start of 'next_step_id'.
    """
    assert result["type"] == FlowResultType.MENU  # type: ignore
    assert result["step_id"] == menu_step_id  # type: ignore
    result = await flow.async_configure(
        result["flow_id"],
        user_input={"next_step_id": next_step_id},
    )
    assert result["type"] == next_step_type  # type: ignore
    if next_step_type == FlowResultType.FORM:
        assert result["step_id"] == next_step_id  # type: ignore
    return result


class DictMatcher(dict):
    """
    customize dictionary matching by checking if
    only the keys defined in this object are matched in the
    compared one. It works following the same assumptions as for the ANY
    symbol in the mock library
    """

    def __eq__(self, other):
        for key, value in self.items():
            if value != other.get(key):
                return False
        return True


class MessageMatcher:
    """
    Helper useful when checking the meross messages.
    This is expecially helpful when asserting mock calls where the argument(s)
    is a Meross message dict. Most of the times we cannot execute a perfect match
    nor it is desirable but we just want to be sure some dict key:value pairs are
    set correctly
    """

    def __init__(self, *, header=ANY, payload=ANY):
        self.header = header
        self.payload = payload

    def __eq__(self, reply):
        reply = json_loads(reply)
        # here self.header and self.payload are likely DictMatcher objects
        # in order to chek against some required and stable keys in the message
        return (self.header == reply[mc.KEY_HEADER]) and (
            self.payload == reply[mc.KEY_PAYLOAD]
        )


class TimeMocker(contextlib.AbstractContextManager):
    """
    time mocker helper using freeztime and providing some helpers
    to integrate time changes with HA core mechanics.
    At the time, don't use it together with DeviceContext which
    mocks its own time
    """

    time: FrozenDateTimeFactory | StepTickTimeFactory

    __slots__ = (
        "hass",
        "time",
        "_freeze_time",
        "_warp_task",
        "_warp_run",
    )

    def __init__(self, hass: HomeAssistant, time_to_freeze=None):
        super().__init__()
        self.hass = hass
        self._freeze_time = freeze_time(time_to_freeze)
        self._warp_task: Future | None = None
        self._warp_run = False
        hass.loop.slow_callback_duration = 2.1

    def __enter__(self):
        self.time = self._freeze_time.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._freeze_time.stop()

    def tick(self, tick: timedelta | float | int):
        self.time.tick(tick if isinstance(tick, timedelta) else timedelta(seconds=tick))
        async_fire_time_changed_exact(self.hass)

    async def async_tick(self, tick: timedelta | float | int):
        self.time.tick(tick if isinstance(tick, timedelta) else timedelta(seconds=tick))
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

        """
        This basic time ticking doesn't produce fixed time steps increase
        since the time mocker might also be externally manipulated
        (for example our HTTP mocker introduces a delay)
        This introduces a sort of 'drift' in our time sampling
        while we'd better prefer having stable fixed time steps

        while self.time() < timeout:
            await self.async_tick(tick)

        The following solution instead creates it's own time ramp
        and forces the time mocker to follow our prefixed sampling
        even if it was 'ticked' in other parts of the code

        beware though that the additional ticks don't overflow
        our sampling step tick...as the time could then jump back
        according to this algorithm and might be undesirable
        (to say the least - dunno if freezegun allows this)
        """
        time_current = self.time()
        time_next = time_current + tick
        tick_next = tick
        while time_current < timeout:
            await self.async_tick(tick_next)
            # here self.time() might have been advanced more than tick
            time_current = time_next
            time_next = time_current + tick
            tick_next = time_next - self.time()

    def warp(self, tick: float | int | timedelta = 0.5):
        """
        starts an asynchronous task in an executor which manipulates our
        freze_time so the time passes and get advanced to
        time.time() + timeout.
        While passing it tries to perform HA events rollout
        every tick seconds
        """
        assert self._warp_task is None

        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        def _warp():
            print("TimeMocker.warp: entering executor")
            count = 0
            while self._warp_run:
                _time = self.time()
                run_coroutine_threadsafe(self.async_tick(tick), self.hass.loop)
                while _time == self.time():
                    time.sleep(0.01)
                count += 1
            print(f"TimeMocker.warp: exiting executor (_warp count={count})")

        self._warp_run = True
        self._warp_task = self.hass.async_add_executor_job(_warp)

    async def async_stopwarp(self):
        print("TimeMocker.warp: stopping executor")
        assert self._warp_task
        self._warp_run = False
        await self._warp_task
        self._warp_task = None


class ConfigEntryMocker(contextlib.AbstractAsyncContextManager):
    __slots__ = (
        "hass",
        "config_entry",
        "config_entry_id",
        "auto_setup",
    )

    def __init__(
        self,
        hass: HomeAssistant,
        unique_id: str,
        *,
        data: Any | None = None,
        auto_add: bool = True,
        auto_setup: bool = True,
    ) -> None:
        super().__init__()
        self.hass: Final = hass
        self.config_entry: Final = MockConfigEntry(
            domain=mlc.DOMAIN,
            data=data,
            version=ConfigFlow.VERSION,
            minor_version=ConfigFlow.MINOR_VERSION,
            unique_id=unique_id,
        )
        self.config_entry_id: Final = self.config_entry.entry_id
        self.auto_setup = auto_setup
        if auto_add:
            self.config_entry.add_to_hass(hass)

    @property
    def api(self) -> MerossApi:
        return self.hass.data[mlc.DOMAIN]

    @property
    def manager(self):
        return self.api.managers[self.config_entry_id]

    @property
    def config_entry_loaded(self):
        return self.config_entry.state == config_entries.ConfigEntryState.LOADED

    async def async_setup(self):
        result = await self.hass.config_entries.async_setup(self.config_entry_id)
        await self.hass.async_block_till_done()
        return result

    async def async_unload(self):
        result = await self.hass.config_entries.async_unload(self.config_entry_id)
        await self.hass.async_block_till_done()
        return result

    async def async_test_config_entry_diagnostics(self):
        assert self.config_entry_loaded
        diagnostic = await async_get_config_entry_diagnostics(
            self.hass, self.config_entry
        )
        assert diagnostic

    async def __aenter__(self):
        if self.auto_setup:
            assert await self.async_setup()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        if self.config_entry.state.recoverable:
            assert await self.async_unload()
        return None


class MQTTHubEntryMocker(ConfigEntryMocker):
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        data=tc.MOCK_HUB_CONFIG,
        auto_add: bool = True,
        auto_setup: bool = True,
    ):
        super().__init__(
            hass,
            mlc.DOMAIN,
            data=data,
            auto_add=auto_add,
            auto_setup=auto_setup,
        )


class ProfileEntryMocker(ConfigEntryMocker):
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        data=tc.MOCK_PROFILE_CONFIG,
        auto_add: bool = True,
        auto_setup: bool = True,
    ):
        super().__init__(
            hass,
            f"profile.{data[mc.KEY_USERID_]}",
            data=data,
            auto_add=auto_add,
            auto_setup=auto_setup,
        )


def build_emulator(
    model: str, *, device_id=tc.MOCK_DEVICE_UUID, key=tc.MOCK_KEY
) -> MerossEmulator:
    # Watchout: this call will not use the uuid and key set
    # in the filename, just DEFAULT_UUID and DEFAULT_KEY
    return emulator_build_emulator(
        tc.EMULATOR_TRACES_PATH + tc.EMULATOR_TRACES_MAP[model],
        device_id,
        key,
    )


def build_emulator_for_profile(
    profile_config: mlc.ProfileConfigType,
    *,
    model: str | None = None,
    device_id=tc.MOCK_DEVICE_UUID,
) -> MerossEmulator:
    """
    This call will setup the emulator patching its configuration
    in order to be 'binded' to the provided cloud profile data.
    Specifying a 'model' will try to match a suitable deviceType
    in the stored profile else it will just default
    """
    domain = None
    reservedDomain = None
    userid = profile_config[mc.KEY_USERID_]
    key = profile_config[mc.KEY_KEY]

    cloudprofiledata = tc.MOCK_PROFILE_STORAGE.get(f"{mlc.DOMAIN}.profile.{userid}")
    if cloudprofiledata:
        cloudprofiledata = cloudprofiledata["data"]
        device_info_dict = cloudprofiledata.get("deviceInfo")
        if device_info_dict:
            for device_info in cloudprofiledata["deviceInfo"].values():
                device_type = device_info[mc.KEY_DEVICETYPE]  # type: ignore
                if model and (model != device_type):
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
    fw[mc.KEY_USERID] = int(userid)

    if domain:
        broker = HostAddress.build(domain)
        fw[mc.KEY_SERVER] = broker.host
        fw[mc.KEY_PORT] = broker.port

    if reservedDomain:
        if domain == reservedDomain:
            fw.pop(mc.KEY_SECONDSERVER, None)
            fw.pop(mc.KEY_SECONDPORT, None)
        else:
            broker = HostAddress.build(reservedDomain)
            fw[mc.KEY_SECONDSERVER] = broker.host
            fw[mc.KEY_SECONDPORT] = broker.port

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
        mlc.CONF_TRACE_TIMEOUT: tc.MOCK_TRACE_TIMEOUT,
    }

    if config_data:
        data.update(config_data)

    return data


class EmulatorContext(contextlib.AbstractContextManager):
    def __init__(
        self,
        emulator: MerossEmulator | str,
        aioclient_mock: AiohttpClientMocker,
        *,
        frozen_time: FrozenDateTimeFactory | StepTickTimeFactory | None = None,
        host: str | None = None,
    ) -> None:
        if isinstance(emulator, str):
            emulator = build_emulator(emulator)
        self.emulator = emulator
        self.host = host or str(id(emulator))
        self.aioclient_mock = aioclient_mock
        if frozen_time:
            self.frozen_time = frozen_time
            emulator.update_epoch()
        else:
            self.frozen_time = None

    def __enter__(self):
        self.aioclient_mock.post(
            f"http://{self.host}/config",
            side_effect=self._handle_http_request,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.aioclient_mock.clear_requests()
        self.emulator.shutdown()
        return None

    async def _handle_http_request(self, method, url, data):
        response = self.emulator.handle(data)
        if self.frozen_time:
            # emulate http roundtrip time
            self.frozen_time.tick(timedelta(seconds=tc.MOCK_HTTP_RESPONSE_DELAY))
        return AiohttpClientMockResponse(method, url, json=response)


class DeviceContext(ConfigEntryMocker):
    """
    This is a 'full featured' context providing an emulator and setting it
    up as a configured device in HA
    It also provides timefreezing
    """

    __slots__ = (
        "emulator",
        "emulator_context",
        "device_id",
        "_aioclient_mock",
        "_time_mock",
        "_time_mock_owned",
        "_exception_warning_patcher",
        "exception_warning_mock",
    )

    def __init__(
        self,
        hass: HomeAssistant,
        emulator: MerossEmulator | str,
        aioclient_mock: AiohttpClientMocker,
        *,
        time: TimeMocker | datetime | None = None,
        config_data: dict | None = None,
    ):
        if isinstance(emulator, str):
            emulator = build_emulator(emulator)
        super().__init__(
            hass,
            emulator.uuid,
            data=build_emulator_config_entry(emulator, config_data=config_data),
            auto_add=True,
            auto_setup=False,
        )
        self.emulator = emulator
        self.device_id = emulator.uuid
        self._aioclient_mock = aioclient_mock
        if isinstance(time, TimeMocker):
            self._time_mock = time
            self._time_mock_owned = False
        else:
            self._time_mock = TimeMocker(hass, time)
            self._time_mock_owned = True

    @property
    def device(self) -> MerossDevice:
        return self.api.devices[self.device_id]  # type: ignore

    @property
    def time(self):
        return self._time_mock.time

    async def __aenter__(self):
        if self._time_mock_owned:
            self._time_mock.__enter__()
        self.emulator_context = EmulatorContext(
            self.emulator, self._aioclient_mock, frozen_time=self._time_mock.time
        )
        self.emulator_context.__enter__()

        def _patch_loggable_log_exception(
            level: int, exception: Exception, msg: str, *args, **kwargs
        ):
            raise Exception(
                f"log_exception called while testing {self.device_id}"
            ) from exception

        self._exception_warning_patcher = patch.object(
            Loggable,
            "log_exception",
            side_effect=_patch_loggable_log_exception,
        )
        self.exception_warning_mock = self._exception_warning_patcher.start()

        return await super().__aenter__()

    async def __aexit__(self, exc_type, exc_value: BaseException | None, traceback):
        try:
            return await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            self._exception_warning_patcher.stop()
            self.emulator_context.__exit__(exc_type, exc_value, traceback)
            if self._time_mock_owned:
                self._time_mock.__exit__(exc_type, exc_value, traceback)
            if exc_value:
                exc_value.args = (*exc_value.args, self.emulator.uuid)

    async def perform_coldstart(self):
        """
        to be called after setting up a device (context) to actually
        execute the cold-start polling sequence.
        After this the device should be online and all the polling
        namespaces done
        """
        if not self.config_entry_loaded:
            await self.async_setup()
        assert (device := self.device) and not device.online
        await self.async_tick(timedelta(seconds=mlc.PARAM_COLDSTARTPOLL_DELAY))
        assert device.online
        return device

    async def async_setup(self):
        assert not MerossApi.devices.get(self.device_id)
        assert not self.config_entry_loaded
        result = await super().async_setup()
        assert (device := self.device) and not device.online
        return result

    async def async_unload(self):
        """
        Robust finalizer, asserts the config_entry will be correctly unloaded
        and the device cleanup done, whatever the config_entry.state
        """
        result = await super().async_unload()
        assert not MerossApi.devices.get(self.device_id)
        return result

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
        # (re)online the device
        return await self.perform_coldstart()

    async def async_tick(self, tick: timedelta | float | int):
        await self._time_mock.async_tick(tick)

    async def async_move_to(self, target_datetime: datetime):
        await self._time_mock.async_move_to(target_datetime)

    async def async_warp(
        self,
        timeout: float | int | timedelta | datetime,
        tick: float | int | timedelta = 1,
    ):
        await self._time_mock.async_warp(timeout, tick)

    def warp(self, tick: float | int | timedelta = 0.5):
        self._time_mock.warp(tick)

    async def async_stopwarp(self):
        await self._time_mock.async_stopwarp()

    async def async_poll_single(self):
        """Advances the time mocker up to the next polling cycle and executes it."""
        await self._time_mock.async_tick(
            self.device._unsub_polling_callback.when() - self.hass.loop.time()  # type: ignore
        )

    async def async_poll_timeout(
        self,
        timeout: float | int | timedelta | datetime,
    ):
        """Advances the time mocker up to the timeout (delta or absolute)
        stepping exactly through each single polling loop."""
        if not isinstance(timeout, datetime):
            if isinstance(timeout, timedelta):
                timeout = self.time() + timeout
            else:
                timeout = self.time() + timedelta(seconds=timeout)

        while self.time() < timeout:
            await self.async_poll_single()


class CloudApiMocker(contextlib.AbstractContextManager):
    """
    Emulates the Meross server side api by leveraging aioclient_mock
    """

    def __init__(self, aioclient_mock: AiohttpClientMocker, online: bool = True):
        self.aioclient_mock = aioclient_mock
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
        return None

    @staticmethod
    def _validate_request_payload(data) -> dict:
        if not isinstance(data, dict):
            data = json_loads(data)
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
        return json_loads(params)

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
            response[mc.KEY_DATA] = tc.MOCK_PROFILE_CREDENTIALS_LOGIN
        return response

    def _v1_auth_signin(self, request: dict):

        if mc.KEY_EMAIL not in request:
            return {mc.KEY_APISTATUS: cloudapi.APISTATUS_INVALID_EMAIL}
        elif request[mc.KEY_EMAIL] != tc.MOCK_PROFILE_EMAIL:
            return {mc.KEY_APISTATUS: cloudapi.APISTATUS_UNEXISTING_ACCOUNT}
        elif mc.KEY_PASSWORD not in request:
            return {mc.KEY_APISTATUS: cloudapi.APISTATUS_MISSING_PASSWORD}
        elif (
            request[mc.KEY_PASSWORD]
            != hashlib.md5(tc.MOCK_PROFILE_PASSWORD.encode("utf8")).hexdigest()
        ):
            return {mc.KEY_APISTATUS: cloudapi.APISTATUS_WRONG_CREDENTIALS}
        else:
            return {
                mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR,
                mc.KEY_DATA: tc.MOCK_PROFILE_CREDENTIALS_SIGNIN.copy(),
            }
        return response

    def _v1_device_devlist(self, request: dict):
        assert len(request) == 0
        return {
            mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR,
            mc.KEY_DATA: [item.copy() for item in tc.MOCK_CLOUDAPI_DEVICE_DEVLIST],
        }

    def _v1_device_latestversion(self, request: dict):
        assert len(request) == 0
        return {
            mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR,
            mc.KEY_DATA: [
                item.copy() for item in tc.MOCK_CLOUDAPI_DEVICE_LATESTVERSION
            ],
        }

    def _v1_hub_getsubdevices(self, request: dict):
        if mc.KEY_UUID not in request:
            return {mc.KEY_APISTATUS: -1, mc.KEY_INFO: "Missing uuid in request"}
        else:
            uuid = request[mc.KEY_UUID]
            if uuid not in tc.MOCK_CLOUDAPI_HUB_GETSUBDEVICES:
                return {mc.KEY_APISTATUS: -1, mc.KEY_INFO: "uuid not registered"}
            else:
                return {
                    mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR,
                    mc.KEY_DATA: [
                        item.copy() for item in tc.MOCK_CLOUDAPI_HUB_GETSUBDEVICES[uuid]
                    ],
                }

    def _v1_profile_logout(self, request: dict):
        assert len(request) == 0
        return {mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR, mc.KEY_DATA: {}}


MqttMockPahoClient = MagicMock
"""MagicMock for `paho.mqtt.client.Client`"""
MqttMockHAClient = MagicMock
"""MagicMock for `homeassistant.components.mqtt.MQTT`."""
MqttMockHAClientGenerator = Callable[..., Coroutine[Any, Any, MqttMockHAClient]]


class HAMQTTMocker(contextlib.AbstractAsyncContextManager):
    def __init__(self):
        self.async_publish_patcher = patch(
            "homeassistant.components.mqtt.async_publish"
        )

    async def __aenter__(self):
        """Return `self` upon entering the runtime context."""
        self.async_publish_mock = self.async_publish_patcher.start()
        self.async_publish_mock.side_effect = self._async_publish
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.async_publish_patcher.stop()
        return None

    async def _async_publish(
        self, hass: HomeAssistant, topic: str, payload: str, *args, **kwargs
    ):
        pass


class MerossMQTTMocker(contextlib.AbstractContextManager):
    def __init__(self, hass: HomeAssistant):
        def _safe_start(_self: MerossMQTTConnection, *args, **kwargs):
            """this runs in an executor"""
            _self._stateext = _self.STATE_CONNECTED
            hass.add_job(_self._mqtt_connected)

        self.safe_start_patcher = patch.object(
            MerossMQTTConnection,
            "safe_start",
            autospec=True,
            side_effect=_safe_start,
        )

        def _safe_stop(_self: MerossMQTTConnection, *args, **kwargs):
            """this runs in an executor"""
            _self._stateext = _self.STATE_DISCONNECTED
            hass.add_job(_self._mqtt_disconnected)

        self.safe_stop_patcher = patch.object(
            MerossMQTTConnection,
            "safe_stop",
            autospec=True,
            side_effect=_safe_stop,
        )

        async def _async_mqtt_publish(_self: MerossMQTTConnection, *args, **kwargs):
            return None

        self.async_mqtt_publish_patcher = patch.object(
            MerossMQTTConnection,
            "async_mqtt_publish",
            autospec=True,
            side_effect=_async_mqtt_publish,
        )

    def __enter__(self):
        self.safe_start_mock = self.safe_start_patcher.start()
        self.safe_stop_mock = self.safe_stop_patcher.start()
        self.async_mqtt_publish_mock = self.async_mqtt_publish_patcher.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.safe_start_mock:
            self.safe_start_patcher.stop()
        if self.safe_stop_mock:
            self.safe_stop_patcher.stop()
        if self.async_mqtt_publish_mock:
            self.async_mqtt_publish_patcher.stop()
        return None
