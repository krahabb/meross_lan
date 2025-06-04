import asyncio
import base64
from collections import namedtuple
import contextlib
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import logging
import re
import time
import typing
from unittest.mock import ANY, MagicMock, patch

import aiohttp
from freezegun.api import freeze_time
from homeassistant import config_entries, const as hac
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed_exact,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from custom_components.meross_lan import const as mlc
from custom_components.meross_lan.config_flow import ConfigFlow
from custom_components.meross_lan.diagnostics import async_get_config_entry_diagnostics
from custom_components.meross_lan.helpers import Loggable
from custom_components.meross_lan.helpers.meross_profile import (
    MerossMQTTConnection,
    MQTTConnection,
)
from custom_components.meross_lan.merossclient import (
    cloudapi,
    const as mc,
    json_loads,
)
from emulator import MerossEmulator, build_emulator as emulator_build_emulator

from . import const as tc

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        ClassVar,
        Coroutine,
        Final,
        Iterable,
        Mapping,
        NotRequired,
        TypedDict,
        Unpack,
    )

    from freezegun.api import (
        FrozenDateTimeFactory,
        StepTickTimeFactory,
        TickingDateTimeFactory,
        _Freezable,
    )

    _TimeFactory = FrozenDateTimeFactory | StepTickTimeFactory | TickingDateTimeFactory

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    MqttMockPahoClient = MagicMock
    """MagicMock for `paho.mqtt.client.Client`"""
    MqttMockHAClient = MagicMock
    """MagicMock for `homeassistant.components.mqtt.MQTT`."""
    MqttMockHAClientGenerator = Callable[..., Coroutine[Any, Any, MqttMockHAClient]]

    from pytest import CaptureFixture, FixtureRequest, LogCaptureFixture

    from custom_components.meross_lan.helpers.component_api import ComponentApi
    from custom_components.meross_lan.helpers.device import Device
    from custom_components.meross_lan.helpers.manager import ConfigEntryManager
    from custom_components.meross_lan.merossclient import MerossMessage, MerossResponse

LOGGER = logging.getLogger("meross_lan.tests")


async def async_assert_flow_menu_to_step(
    flow: config_entries.ConfigEntriesFlowManager | config_entries.OptionsFlowManager,
    result: config_entries.ConfigFlowResult,
    menu_step_id: str,
    next_step_id: str,
    next_step_type: FlowResultType = FlowResultType.FORM,
):
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


class LoggableException(contextlib.AbstractContextManager):

    raise_on_log_exception: bool

    __slots__ = (
        "raise_on_log_exception",
        "_patch",
        "_mock",
        "_log_exception_old",
    )

    def __init__(self, raise_on_log_exception=True):
        self.raise_on_log_exception = raise_on_log_exception
        self._log_exception_old = Loggable.log_exception
        self._patch = patch.object(
            Loggable,
            "log_exception",
            autospec=True,
            side_effect=self._patch_log_exception,
        )

    def __enter__(self):
        self._mock = self._patch.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._patch.stop()

    def _patch_log_exception(
        self,
        loggable: Loggable,
        level: int,
        exception: Exception,
        msg: str,
        *args,
        **kwargs,
    ):
        self._log_exception_old(loggable, level, exception, msg, *args, **kwargs)
        LOGGER.warning(
            f"Loggable.log_exception called with: loggable={loggable} level={level} exception={exception}",
        )


class TimeMocker(contextlib.AbstractContextManager):
    """
    time mocker helper using freeztime and providing some helpers
    to integrate time changes with HA core mechanics.
    At the time, don't use it together with DeviceContext which
    mocks its own time
    """

    time: "_TimeFactory"

    __slots__ = (
        "hass",
        "time",
        "_freeze_time",
        "_warp_task",
        "_warp_run",
    )

    def __init__(
        self, hass: "HomeAssistant", time_to_freeze: "_Freezable | None" = None
    ):
        super().__init__()
        self.hass = hass
        self._freeze_time = freeze_time(time_to_freeze)
        self._warp_task: asyncio.Future | None = None
        self._warp_run = False
        hass.loop.slow_callback_duration = 2.1

    def __enter__(self):
        self.time = self._freeze_time.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._freeze_time.stop()

    def __call__(self):
        return self.time()

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
        while time_current < timeout:
            await self.async_move_to(time_next)
            # here self.time() might have been advanced more than tick
            time_current = self.time()
            time_next = time_next + tick

    async def async_warp_iterator(
        self,
        timeout: float | int | timedelta | datetime,
        tick: float | int | timedelta = 1,
    ):
        """generator version of async time warping (async_warp)"""
        if not isinstance(timeout, datetime):
            if isinstance(timeout, timedelta):
                timeout = self.time() + timeout
            else:
                timeout = self.time() + timedelta(seconds=timeout)
        if not isinstance(tick, timedelta):
            tick = timedelta(seconds=tick)

        time_current = self.time()
        time_next = time_current + tick
        while time_current < timeout:
            await self.async_move_to(time_next)
            # here self.time() might have been advanced more than tick
            time_current = self.time()
            time_next = time_next + tick
            yield time_current

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
            count = 0
            while self._warp_run:
                _time = self.time()
                asyncio.run_coroutine_threadsafe(self.async_tick(tick), self.hass.loop)
                while _time == self.time():
                    time.sleep(0.01)
                count += 1

        self._warp_run = True
        self._warp_task = self.hass.async_add_executor_job(_warp)

    async def async_stopwarp(self):
        assert self._warp_task
        self._warp_run = False
        await self._warp_task
        self._warp_task = None


class LogManager:

    MEROSS_LAN_LOGGER: "Final" = r"custom_components\.meross_lan.*"

    if typing.TYPE_CHECKING:

        class Args(TypedDict):
            pass

        request: Final[FixtureRequest]
        capsys: Final[CaptureFixture | None]  # type: ignore
        caplog: Final[LogCaptureFixture | None]  # type: ignore

        class LogMatchArgs(TypedDict):
            level: NotRequired[int]
            name: NotRequired[str]
            message: NotRequired[str]

    LogMatchTuple = namedtuple(
        "LogMatchArgsTuple",
        "message, level, name",
        defaults=(logging.WARNING, MEROSS_LAN_LOGGER),
    )

    IGNORED_LOGS: "ClassVar[Iterable[LogMatchTuple]]" = ()
    """List of logs to be automatically ignored when dumping
     the WARNINGS in flush_logs."""

    OPTIONAL_FIXTURES: "ClassVar" = ["capsys", "caplog"]

    __slots__ = ["request"] + OPTIONAL_FIXTURES

    def __init__(self, request: "FixtureRequest"):
        self.request = request
        for fixture in self.__class__.OPTIONAL_FIXTURES:
            setattr(
                self,
                fixture,
                (
                    (
                        request.getfixturevalue(fixture)
                        if fixture in request.fixturenames
                        else None
                    )
                    if request
                    else None
                ),
            )

    def pop_logs(self, **kwargs: "Unpack[LogMatchArgs]"):
        if caplog := self.caplog:
            level = kwargs.get("level")
            p_name = re.compile(kwargs.get("name", LogManager.MEROSS_LAN_LOGGER))
            p_message = re.compile(kwargs.get("message", r".*"))
            records = caplog.records
            if level:
                pop = [
                    record
                    for record in records
                    if record.levelno == level
                    and p_name.match(record.name)
                    and p_message.match(record.message)
                ]
            else:
                pop = [
                    record
                    for record in records
                    if p_name.match(record.name) and p_message.match(record.message)
                ]

            for record in pop:
                records.remove(record)
            return pop
        else:
            return []

    def assert_logs(self, count: int, **kwargs: "Unpack[LogMatchArgs]"):
        logs = self.pop_logs(**kwargs)
        assert logs and len(logs) == count, "Inconsistent logging output"

    def flush_logs(self, context_tag: str):
        if (capsys := self.capsys) and (caplog := self.caplog):
            for ignored in self.__class__.IGNORED_LOGS:
                self.pop_logs(**ignored._asdict())
            # this might be overkill since this code only runs in 'call' phase
            phases = ("setup", "call", "teardown")
            phase_records = {phase: caplog.get_records(phase) for phase in phases}
            messages = []
            for phase, records in phase_records.items():
                meross_lan_records = [
                    record
                    for record in records
                    if record.levelno >= logging.WARNING
                    and record.name.startswith("custom_components.meross_lan")
                ]

                def _pop_record(record):
                    # eat up from caplog context (only) the records we're going
                    # to print out so that other managers can inspect the remaining.
                    records.remove(record)
                    return record.message

                messages += [
                    (phase, _pop_record(record)) for record in meross_lan_records
                ]
                phase_records[phase] = meross_lan_records

            if messages:
                with capsys.disabled():
                    print(f"\n{self.request.node.name}: WARNINGS in {context_tag}")
                    print(*messages, sep="\n")


class ConfigEntryMocker(contextlib.AbstractAsyncContextManager, LogManager):

    if typing.TYPE_CHECKING:

        class Args(TypedDict):
            data: NotRequired[Mapping[str, Any]]
            auto_add: NotRequired[bool]
            auto_setup: NotRequired[bool]

        hass: Final[HomeAssistant]
        config_entry: Final[ConfigEntry[ConfigEntryManager]]
        config_entry_id: Final
        auto_setup: Final

    __slots__ = (
        "hass",
        "config_entry",
        "config_entry_id",
        "auto_setup",
    )

    def __init__(
        self,
        request: "FixtureRequest",
        hass: "HomeAssistant",
        unique_id: str,
        title: str,
        **kwargs: "Unpack[Args]",
    ) -> None:
        super().__init__(request)
        self.hass = hass
        config_entry_kwargs = {
            "domain": mlc.DOMAIN,
            "data": kwargs.get("data"),
            "version": ConfigFlow.VERSION,
            "unique_id": unique_id,
            "title": title,
        }
        if hac.MAJOR_VERSION >= 2024:
            config_entry_kwargs["minor_version"] = ConfigFlow.MINOR_VERSION
        self.config_entry = MockConfigEntry(**config_entry_kwargs)
        self.config_entry_id = self.config_entry.entry_id
        self.auto_setup = kwargs.get("auto_setup", True)
        if kwargs.get("auto_add", True):
            self.config_entry.add_to_hass(hass)

    @property
    def api_loaded(self):
        return mlc.DOMAIN in self.hass.data

    @property
    def api(self) -> "ComponentApi":
        """Beware unsafe access: ensure the component is currently loaded"""
        return self.hass.data[mlc.DOMAIN]

    @property
    def manager(self):
        return self.config_entry.runtime_data

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
        self.flush_logs(self.config_entry.title)
        return None


class MQTTHubEntryMocker(ConfigEntryMocker):

    if typing.TYPE_CHECKING:

        class Args(ConfigEntryMocker.Args):
            pass

    def __init__(
        self, request: "FixtureRequest", hass: "HomeAssistant", **kwargs: "Unpack[Args]"
    ):
        if not "data" in kwargs:
            kwargs["data"] = tc.MOCK_HUB_CONFIG
        super().__init__(request, hass, mlc.DOMAIN, "MQTTHub", **kwargs)


class ProfileEntryMocker(ConfigEntryMocker):

    if typing.TYPE_CHECKING:

        class Args(ConfigEntryMocker.Args):
            pass

    def __init__(
        self, request: "FixtureRequest", hass: "HomeAssistant", **kwargs: "Unpack[Args]"
    ):
        if "data" in kwargs:
            data = kwargs["data"]
        else:
            kwargs["data"] = data = tc.MOCK_PROFILE_CONFIG
        super().__init__(
            request,
            hass,
            f"profile.{data[mc.KEY_USERID_]}",
            f"CloudProfile({data[mc.KEY_EMAIL]})",
            **kwargs,
        )


def build_emulator(
    model: str,
    *,
    key: str = tc.MOCK_KEY,
    uuid: str = tc.MOCK_DEVICE_UUID,
    broker: str | None = None,
    userId: int | None = None,
) -> MerossEmulator:
    # Watchout: this call will not use the uuid and key set
    # in the filename, just DEFAULT_UUID and DEFAULT_KEY
    return emulator_build_emulator(
        tc.EMULATOR_TRACES_PATH + tc.EMULATOR_TRACES_MAP[model],
        key=key,
        uuid=uuid,
        broker=broker,
        userId=userId,
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

    return build_emulator(
        model, key=key, uuid=device_id, broker=domain, userId=int(userid)
    )


def build_emulator_config_entry(
    emulator: MerossEmulator, config_data: "Mapping | None" = None
):
    """
    Builds a consistent config_entry for an emulated device with HTTP communication.
    Use config_data to override/add the defaults
    """
    if config_data:
        if mlc.CONF_KEY in config_data:
            emulator.key = config_data[mlc.CONF_KEY]

    data: mlc.DeviceConfigType = {
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
        mlc.CONF_OBFUSCATE: False,
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
        frozen_time: "_TimeFactory | None" = None,
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
        return AiohttpClientMockResponse(method, url, text=response)


class DeviceContext(ConfigEntryMocker):
    """
    This is a 'full featured' context providing an emulator and setting it
    up as a configured device in HA
    It also provides timefreezing
    """

    if typing.TYPE_CHECKING:

        class Args(ConfigEntryMocker.Args):
            time: NotRequired[TimeMocker | datetime | None]

        time: Final[TimeMocker]

    IGNORED_LOGS = (
        LogManager.LogMatchTuple(
            r"Protocol error: namespace:.*not supported in emulator",
        ),
    )

    __slots__ = (
        "emulator",
        "emulator_context",
        "device_id",
        "time",
        "_aioclient_mock",
        "_time_mock_owned",
    )

    def __init__(
        self,
        request: "FixtureRequest",
        hass: "HomeAssistant",
        emulator: MerossEmulator | str,
        aioclient_mock: AiohttpClientMocker,
        **kwargs: "Unpack[Args]",
    ):
        if isinstance(emulator, str):
            emulator = build_emulator(emulator)
        kwargs["data"] = build_emulator_config_entry(
            emulator, config_data=kwargs.get("data")
        )
        descriptor = emulator.descriptor
        kwargs["auto_add"] = True
        kwargs["auto_setup"] = False
        super().__init__(
            request,
            hass,
            emulator.uuid,
            f"Device({descriptor.productname}-{descriptor.productmodel})",
            **kwargs,
        )
        self.emulator = emulator
        self.device_id = emulator.uuid
        self._aioclient_mock = aioclient_mock
        time = kwargs.get("time")
        if isinstance(time, TimeMocker):
            self.time = time
            self._time_mock_owned = False
        else:
            self.time = TimeMocker(hass, time)
            self._time_mock_owned = True

    @property
    def device(self) -> "Device":
        return self.api.devices[self.device_id]  # type: ignore

    async def __aenter__(self):
        if self._time_mock_owned:
            self.time.__enter__()
        self.emulator_context = EmulatorContext(
            self.emulator, self._aioclient_mock, frozen_time=self.time.time
        )
        self.emulator_context.__enter__()
        return await super().__aenter__()

    async def __aexit__(self, exc_type, exc_value: BaseException | None, traceback):
        try:
            return await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            self.emulator_context.__exit__(exc_type, exc_value, traceback)
            if self._time_mock_owned:
                self.time.__exit__(exc_type, exc_value, traceback)
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
        await self.time.async_tick(timedelta(seconds=mlc.PARAM_COLDSTARTPOLL_DELAY))
        assert device.online
        return device

    async def async_setup(self):
        assert not self.config_entry_loaded
        assert not (self.api_loaded and self.api.devices.get(self.device_id))
        result = await super().async_setup()
        assert (device := self.device) and not device.online
        return result

    async def async_unload(self):
        """
        Robust finalizer, asserts the config_entry will be correctly unloaded
        and the device cleanup done, whatever the config_entry.state
        """
        result = await super().async_unload()
        assert not (self.api_loaded and self.api.devices.get(self.device_id))
        return result

    async def async_enable_entity(self, entity_id):
        # entity enable will reload the config_entry
        # by firing a trigger event which will the be collected by
        # config_entries
        # so we have to recover the right instances
        ent_reg = er.async_get(self.hass)
        ent_reg.async_update_entity(entity_id, disabled_by=None)
        # fire the entity registry changed
        await self.hass.async_block_till_done()
        # perform the reload task after RELOAD_AFTER_UPDATE_DELAY
        await self.time.async_tick(
            timedelta(seconds=config_entries.RELOAD_AFTER_UPDATE_DELAY)
        )
        # (re)online the device
        return await self.perform_coldstart()

    async def async_poll_single(self):
        """Advances the time mocker up to the next polling cycle and executes it."""
        await self.time.async_tick(
            self.device._polling_callback_unsub.when() - self.hass.loop.time()  # type: ignore
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
        self.online = online
        self.api_calls: dict[str, int] = {}
        aioclient_mock.post(
            re.compile(r"https://iot\.meross\.com"),
            side_effect=self._async_handle,
        )

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
        if self.online:
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

    def _v1_device_devlist(self, request: dict):
        assert len(request) == 0
        return {
            mc.KEY_APISTATUS: cloudapi.APISTATUS_NO_ERROR,
            mc.KEY_DATA: [
                item.copy() for item in tc.MOCK_CLOUDAPI_DEVICE_DEVLIST.values()
            ],
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


class MQTTConnectionMocker(contextlib.AbstractContextManager):
    def __init__(self, hass: "HomeAssistant"):

        async def _async_mqtt_publish(
            _self: MQTTConnection, device_id: str, request: "MerossMessage"
        ) -> "MerossResponse | None":
            return None

        self.async_mqtt_publish_patcher = patch.object(
            MQTTConnection,
            "async_mqtt_publish",
            autospec=True,
            side_effect=_async_mqtt_publish,
        )

        async def _async_identify_device(
            _self: MQTTConnection, device_id: str, key: str
        ) -> mlc.DeviceConfigType:
            try:
                device_info = tc.MOCK_CLOUDAPI_DEVICE_DEVLIST[device_id]
                emulator = build_emulator_for_profile(
                    tc.MOCK_PROFILE_CONFIG,
                    model=device_info.get(mc.KEY_DEVICETYPE),
                    device_id=device_id,
                )
                device_config = build_emulator_config_entry(emulator)
                return device_config
            except KeyError as e:
                raise Exception(
                    f"MQTTConnectionMocker: unknown device (uuid:{device_id})"
                ) from e

        self.async_identify_device_patcher = patch.object(
            MQTTConnection,
            "async_identify_device",
            autospec=True,
            side_effect=_async_identify_device,
        )

    def __enter__(self):
        self.async_mqtt_publish_mock = self.async_mqtt_publish_patcher.start()
        self.async_identify_device_mock = self.async_identify_device_patcher.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.async_mqtt_publish_mock:
            self.async_mqtt_publish_patcher.stop()
        if self.async_identify_device_mock:
            self.async_identify_device_patcher.stop()
        return None


class HAMQTTMocker(contextlib.AbstractAsyncContextManager):
    def __init__(self, hass: "HomeAssistant"):
        self.hass = hass
        self.async_publish_patcher = patch(
            "homeassistant.components.mqtt.async_publish"
        )

    async def __aenter__(self):
        self.async_publish_mock = self.async_publish_patcher.start()
        self.async_publish_mock.side_effect = self._async_publish
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.async_publish_patcher.stop()
        api: "ComponentApi" = self.hass.data[mlc.DOMAIN]
        if api and api._mqtt_connection:
            from homeassistant.components.mqtt.client import UNSUBSCRIBE_COOLDOWN

            await api._mqtt_connection.async_mqtt_unsubscribe()
            await asyncio.sleep(UNSUBSCRIBE_COOLDOWN)

        return None

    async def _async_publish(
        self, hass: "HomeAssistant", topic: str, payload: str, *args, **kwargs
    ):
        pass


class MerossMQTTMocker(MQTTConnectionMocker):
    def __init__(self, hass: "HomeAssistant"):
        super().__init__(hass)

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

    def __enter__(self):
        self.safe_start_mock = self.safe_start_patcher.start()
        self.safe_stop_mock = self.safe_stop_patcher.start()
        return super().__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        if self.safe_start_mock:
            self.safe_start_patcher.stop()
        if self.safe_stop_mock:
            self.safe_stop_patcher.stop()
        return super().__exit__(exc_type, exc_value, traceback)
