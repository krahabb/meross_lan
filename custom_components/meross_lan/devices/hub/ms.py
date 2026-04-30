from typing import TYPE_CHECKING, override

from . import SubDevice, mc, mlc, mn, mn_h
from ...binary_sensor import BinarySensorEntity, BinarySensorParser
from ...button import Button
from ...number import NumberParser
from ...select import SelectParser
from ...sensor import EnumParser, EnumSensorEntity, SensorParser
from ..misc import DeviceCfgParser, SensorLatestXParser

if TYPE_CHECKING:
    from typing import Final, TypedDict, Unpack

    from . import Hub
    from ...helpers.entity import Entity
    from ...merossclient.protocol import types as mt


class SensorSubDevice(SubDevice):

    NS_HUB = (mn_h.Appliance_Hub_Sensor_All,)

    def _parse_all(self, payload: dict, /):
        self._parse_online(payload[mc.KEY_ONLINE])
        if not self.is_connected:
            return
        self._parse_digest_(payload[self.key_digest])


class gs559(SensorSubDevice, EnumParser):
    """TODO: parse this PUSH: it looks like it is an sync event sent by device (see trace uuid 29)
    when something happens.
    "Appliance.Hub.Sensor.Smoke": {
            "lastrequest": 0.0,
            "lastresponse": 1771074280.071836,
            "lastpush": {
                "smokeAlarm": [
                {
                    "event": {
                    "test": {
                        "timestamp": 1771072443,
                        "type": 1
                    }
                    },
                    "id": "1800958E1582"
                }
                ]
            },
            "polling_epoch_next": 1771074580.071836,
            "polling_strategy": null
            },
    """

    if TYPE_CHECKING:
        STATUS_MAP: Final
        MUTE_MAP: Final
        STATUS_ALARM: Final[set[int]]
        STATUS_ERROR: Final[set[int]]
        STATUS_MUTED: Final[set[int]]
        ENTITY_DEFS: Final[dict[str, type[Entity]]]

        binary_sensor_alarm: BinarySensorEntity
        binary_sensor_error: BinarySensorEntity
        binary_sensor_muted: BinarySensorEntity
        sensor_interConn: EnumSensorEntity

    init_ns = mn_h.Appliance_Hub_Sensor_Smoke
    init_key_value = EnumParser.KeyValue(mc.KEY_STATUS)
    _attr_translation_key = "smoke_alarm_status"

    STATUS_MAP = {
        17: "error_temperature",
        18: "error_smoke",
        19: "error_battery",
        20: "error_temperature",
        21: "error_smoke",
        22: "error_battery",
        23: "alarm_test",
        24: "alarm_temperature_high",
        25: "alarm_smoke",
        26: "alarm_temperature_high",
        27: "alarm_smoke",
        170: "ok",
    }
    MUTE_MAP = {17: 20, 18: 21, 19: 22, 24: 26, 25: 27, None: 170}
    STATUS_ALARM = {23, 24, 25, 26, 27}
    STATUS_ERROR = {17, 18, 19, 20, 21, 22}
    STATUS_MUTED = {20, 21, 22, 26, 27}

    ENTITY_DEFS = {
        "binary_sensor_alarm": BinarySensorEntity.DEF(
            entity_key=mc.KEY_ALARM, device_class=BinarySensorEntity.DeviceClass.SAFETY
        ),
        "binary_sensor_error": BinarySensorEntity.DEF(
            entity_key=mc.KEY_ERROR, device_class=BinarySensorEntity.DeviceClass.PROBLEM
        ),
        "binary_sensor_muted": BinarySensorEntity.DEF(entity_key="muted"),
        "sensor_interConn": EnumParser.DEF(entity_key=mc.KEY_INTERCONN),
    }
    __slots__ = ENTITY_DEFS.keys()

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SensorSubDevice.__init__(self, subid, hub, key_digest, model)
        self.unique_id = f"{hub.id}_{subid}_status"  # LEGACY unique_id scheme
        for key, entity_def in self.__class__.ENTITY_DEFS.items():
            setattr(self, key, entity_def(self))
        Button(self, async_press=self.async_mute, name="Mute")
        Button(self, async_press=self.async_test, name="Test")

    def shutdown(self):
        SensorSubDevice.shutdown(self)
        for key in self.__class__.ENTITY_DEFS.keys():
            delattr(self, key)

    @override
    def __call__(self, payload: "mt.hub._gs559 | mt.hub.Sensor_Smoke", /):
        # This (being the default fall-back parser) will parse  *.Sensor.All, *Sensor.Smoke
        # and the 'digest' payload since they have the same structure.
        self.ns_value = value = payload[mc.KEY_STATUS]
        self.update_native_value(self.STATUS_MAP.get(value, value))
        self.binary_sensor_alarm.update_boolean_value(value in self.STATUS_ALARM)
        self.binary_sensor_error.update_boolean_value(value in self.STATUS_ERROR)
        self.binary_sensor_muted.update_boolean_value(value in self.STATUS_MUTED)
        try:
            self.sensor_interConn.update_device_value(payload[mc.KEY_INTERCONN])
        except KeyError:
            pass

    async def async_mute(self, /):
        await self.async_request_value(self.MUTE_MAP.get(self.ns_value, 170))

    async def async_test(self, /):
        await self.async_request_value(23)


class ms100(SensorSubDevice):

    class SensorAdjustNumber(NumberParser):

        @override
        async def async_request_value(self, device_value, /):
            # the SET command on NS_APPLIANCE_HUB_SENSOR_ADJUST works by applying
            # the issued value as a 'delta' to the current configured value i.e.
            # 'new adjust value' = 'current adjust value' + 'issued adjust value'
            # Since the native HA interface async_set_native_value wants to set
            # the 'new adjust value' we have to issue the difference against the
            # currently configured one
            await self.async_request_payload(
                self.key_value.payload(device_value - self.ns_value)
            )
            self.update_device_value(device_value)

    SENSOR_ADJUST_DEFS = {
        mc.KEY_HUMIDITY: SensorAdjustNumber.DEF(
            entity_key="config_adjust_humidity",
            name="Adjust humidity",
            device_class=NumberParser.DeviceClass.HUMIDITY,
            native_min_value=-20,
            native_max_value=20,
            native_step=1,
        ),
        mc.KEY_TEMPERATURE: SensorAdjustNumber.DEF(
            entity_key="config_adjust_temperature",
            name="Adjust temperature",
            device_class=NumberParser.DeviceClass.TEMPERATURE,
            native_min_value=-5,
            native_max_value=5,
            native_step=0.1,
        ),
    }

    NS_HUB = (
        mn_h.Appliance_Hub_Sensor_TempHum,
        mn_h.Appliance_Hub_Sensor_Adjust,
        mn_h.Appliance_Hub_Sensor_Latest,
    )

    # These class cfgs are needed to dynamically customize entities in ms130
    TEMPERATURE_ARGS = SensorParser.TEMPERATURE_ARGS | {"device_scale": 10}

    __slots__ = (
        "sensor_temperature",
        "sensor_humidity",
    )

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SensorSubDevice.__init__(self, subid, hub, key_digest, model)
        self.sensor_temperature = SensorParser(self, **self.TEMPERATURE_ARGS)
        self.sensor_humidity = SensorParser(self, **SensorParser.HUMIDITY_ARGS)

    def shutdown(self):
        SensorSubDevice.shutdown(self)
        del self.sensor_temperature
        del self.sensor_humidity

    @override
    def __call__(self, payload: "mt.hub._ms100 | mt.hub.Sensor_TempHum", /):
        # Parses both the 'digest' payload and the Sensor_TempHum since
        # they share the same relevant keys.
        self._update_sensors(
            payload[mc.KEY_LATESTTEMPERATURE], payload[mc.KEY_LATESTHUMIDITY]
        )

    @override
    def _parse_all(self, payload: "mt.hub.Sensor_All_ms100", /):
        self._parse_online(payload[mc.KEY_ONLINE])
        if self.is_connected:
            self._update_sensors(
                payload[mc.KEY_TEMPERATURE][mc.KEY_LATEST],
                payload[mc.KEY_HUMIDITY][mc.KEY_LATEST],
            )

    def _parse_adjust(self, payload: "mt.hub.Sensor_Adjust"):
        self.parent.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust].swap_parsers(
            self,
            *(
                _entity_def(
                    self,
                    ns=mn_h.Appliance_Hub_Sensor_Adjust,
                    key_value=NumberParser.KeyValue(_key),
                    device_scale=10,
                    ns_value=payload[_key],
                )
                for _key, _entity_def in ms100.SENSOR_ADJUST_DEFS.items()
            ),
        )
        # swap also the update_sensors method to a smarter one
        self._update_sensors = self._update_sensors_adjust

    def _parse_latest(self, payload: "mt.hub.Sensor_Latest"):
        self._update_sensors(
            payload[mc.KEY_TEMPERATURE]["sample"],
            payload[mc.KEY_HUMIDITY]["sample"],
        )

    def _update_sensors(self, temperature: int, humidity: int):
        self.sensor_temperature.update_device_value(temperature)
        self.sensor_humidity.update_device_value(humidity)

    def _update_sensors_adjust(self, temperature: int, humidity: int):
        # when a temp/hum reading changes we're smartly requesting
        # the adjust sooner than scheduled in case the change
        # was due to an adjustment. This method is dynamically installed
        # by _parse_adjust when we have confirmation that ns_adjust is
        # delivering for this device.
        _poll_adjust = bool(self.sensor_temperature.update_device_value(temperature))
        _poll_adjust |= bool(self.sensor_humidity.update_device_value(humidity))
        if _poll_adjust:
            handler = self.parent.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust]
            if handler.last_poll_epoch < (self.parent.last_rx_epoch - 30):
                handler.next_poll_epoch = 0.0


class ms130(ms100):

    TEMPERATURE_ARGS = SensorParser.TEMPERATURE_ARGS
    # Configure parser for Appliance.Config.DeviceCfg:
    # {
    #     "config": {
    #         "calibrateCfg": {"temp": 0, "humi": 0},
    #         "timeCfg": {"am": 2},
    #         "ms130Cfg": {"bl": {"bri": 2, "lv": 4, "sleep": 10}},
    #         "channel": 0,
    #         "subId": "1A00694ACBC7",
    #         "unitCfg": {"tempUnit": 1},
    #     }
    # }

    """
    # TODO: 'lv' value is related to Lux by a mapped function defined in App as:
    [
        [0, 5], # 1
        [5, 15], # 2
        [15, 30], # 3
        [30, 50],
        [50, 75],
        [75, 100],
        [100, 150],
        [150, 200],
        [200, 300],
        [300, 400],
        [400, 500],
        [500, 750],
        [750, 1000],
        [1000, 1250],
        [1250, 1500],
        [1500, 2000],
        [2000, 4000],
        [4000, float("inf")],
    ],
     The mapping in App is done as:
    {
        1: 5 lux,
        2: 15 lux,
        3: 30 lux,
        ...
    }
    """
    KEY_MS130CFG = "ms130Cfg"
    KEY_BL = "bl"  # Backlight
    KEY_BRI = "bri"
    KEY_LV = "lv"
    KEY_SLEEP = "sleep"
    DEVICE_CFG_DEFS = {
        KEY_MS130CFG: {
            KEY_BL: {
                KEY_BRI: SelectParser.DEF(
                    entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MS130CFG}_{KEY_BL}_{KEY_BRI}",
                    name="Backlight brightness",
                    options_map={1: "Low", 2: "Medium", 3: "High"},
                ),
                KEY_LV: NumberParser.DEF(
                    entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MS130CFG}_{KEY_BL}_{KEY_LV}",
                    name="Backlight level",
                    native_min_value=1,
                    native_max_value=18,
                    native_step=1,
                ),
                KEY_SLEEP: NumberParser.DEF(
                    entity_key=f"{mn.Appliance_Config_DeviceCfg.slug}__{KEY_MS130CFG}_{KEY_BL}_{KEY_SLEEP}",
                    name="Backlight sleep",
                    device_class=NumberParser.DeviceClass.DURATION,
                    native_unit_of_measurement=mlc.hac.UnitOfTime.SECONDS,
                    native_min_value=3,
                    native_max_value=30,
                    native_step=1,
                ),
            },
        },
    } | DeviceCfgParser.init_parser_defs  # type: ignore[assignment]

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        ms100.__init__(self, subid, hub, key_digest, model)
        index = mn.IndexType.subId.get(subid, 0, None)
        try:
            # Configure parser for Appliance.Control.Sensor.LatestX:
            # {
            #    "latest": [
            #        {
            #            "data": {
            #                "light": [{"value": 220, "timestamp": 1722349685}],
            #                "temp": [{"value": 2134, "timestamp": 1722349685}],
            #                "humi": [{"value": 670, "timestamp": 1722349685}],
            #            },
            #            "channel": 0,
            #            "subId": "1A00694ACBC7",
            #        }
            #    ]
            # }
            hub.ns_handlers[mn.Appliance_Control_Sensor_LatestX].register_parser(
                SensorLatestXParser(
                    subid,
                    hub,
                    index=index,
                    parsers={
                        mc.KEY_LIGHT: SensorParser(self, **SensorParser.LIGHT_ARGS),
                        mc.KEY_TEMP: self.sensor_temperature,
                        mc.KEY_HUMI: self.sensor_humidity,
                    },
                )
            )
        except KeyError as ke:
            # expected (?) if LatestX not supported by the device firmware
            assert ke.args[0] == mn.Appliance_Control_Sensor_LatestX

        try:
            hub.ns_handlers[mn.Appliance_Config_DeviceCfg].register_parser(
                DeviceCfgParser(
                    subid, hub, index=index, parser_defs=ms130.DEVICE_CFG_DEFS
                )
            )
        except KeyError as ke:
            # if ns not supported by the device firmware
            assert ke.args[0] == mn.Appliance_Config_DeviceCfg

    @override
    def __call__(self, payload: "mt.hub._ms130", /):
        # Parses the 'digest' payload.
        self._update_sensors(payload[mc.KEY_TEMP], payload[mc.KEY_HUMI])


class ms200(SensorSubDevice, BinarySensorParser):
    NS_HUB = (mn_h.Appliance_Hub_Sensor_DoorWindow,)
    init_key_value = BinarySensorParser.KeyValue(mc.KEY_STATUS)
    _attr_device_class = BinarySensorParser.DeviceClass.WINDOW

    @property
    def unique_id(self) -> str | None:
        return f"{self.parent.id}_{self.id}_window"

    @unique_id.setter
    def unique_id(self, value):
        pass


class ms400(SensorSubDevice, BinarySensorParser):
    NS_HUB = (mn_h.Appliance_Hub_Sensor_WaterLeak,)
    init_key_value = BinarySensorParser.KeyValue(mc.KEY_LATESTWATERLEAK)
    _attr_device_class = BinarySensorParser.DeviceClass.SAFETY

    @property
    def unique_id(self) -> str | None:
        return f"{self.parent.id}_{self.id}_waterleak"

    @unique_id.setter
    def unique_id(self, value):
        pass
