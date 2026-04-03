from typing import TYPE_CHECKING, override

from . import SubDevice, mc, mn, mn_h
from ...binary_sensor import BinarySensorEntity, BinarySensorParser
from ...button import Button
from ...number import NumberParser
from ...sensor import EnumParser, SensorParser

if TYPE_CHECKING:
    from typing import Final, TypedDict, Unpack

    from . import Hub
    from ...merossclient.protocol import types as mt


class SensorSubDevice(SubDevice):

    NS_HUB = (mn_h.Appliance_Hub_Sensor_All, *SubDevice.NS_HUB)

    def _parse_all(self, payload: dict, /):
        self._parse_online(payload[mc.KEY_ONLINE])
        if not self.available:
            return
        self._parse_digest_(payload[self.key_digest])


class gs559(SensorSubDevice, EnumParser):
    if TYPE_CHECKING:
        STATUS_MAP: Final
        MUTE_MAP: Final
        STATUS_ALARM: Final[set[int]]
        STATUS_ERROR: Final[set[int]]
        STATUS_MUTED: Final[set[int]]

    init_ns = mn_h.Appliance_Hub_Sensor_Smoke
    init_entity_key = mc.KEY_STATUS
    init_key_value = mc.KEY_STATUS
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

    __slots__ = (
        "binary_sensor_alarm",
        "binary_sensor_error",
        "binary_sensor_muted",
        "sensor_interConn",
    )

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SensorSubDevice.__init__(self, subid, hub, key_digest, model)
        self.binary_sensor_alarm = BinarySensorEntity(
            subid,
            hub,
            entity_key=mc.KEY_ALARM,
            device_class=BinarySensorEntity.DeviceClass.SAFETY,
        )
        self.binary_sensor_error = BinarySensorEntity(
            subid,
            hub,
            entity_key=mc.KEY_ERROR,
            device_class=BinarySensorEntity.DeviceClass.PROBLEM,
        )
        self.binary_sensor_muted = BinarySensorEntity(subid, hub, entity_key="muted")
        self.sensor_interConn = EnumParser(subid, hub, entity_key=mc.KEY_INTERCONN)
        Button(subid, hub, self.async_mute, name="Mute")
        Button(subid, hub, self.async_test, name="Test")

    def _parse(self, payload: "mt.hub._smokeAlarm", /):
        self.device_value = value = payload[mc.KEY_STATUS]
        self.update_native_value(self.STATUS_MAP.get(value, value))
        self.binary_sensor_alarm.update_boolean_value(value in self.STATUS_ALARM)
        self.binary_sensor_error.update_boolean_value(value in self.STATUS_ERROR)
        self.binary_sensor_muted.update_boolean_value(value in self.STATUS_MUTED)
        try:
            self.sensor_interConn.update_device_value(payload[mc.KEY_INTERCONN])
        except KeyError:
            pass

    def shutdown(self):
        SensorSubDevice.shutdown(self)
        del self.binary_sensor_muted
        del self.binary_sensor_error
        del self.binary_sensor_alarm
        del self.sensor_interConn

    async def async_mute(self, /):
        try:
            await self.async_request_payload(
                {
                    mc.KEY_STATUS: self.MUTE_MAP.get(self.device_value, 170),
                }
            )
        except KeyError as e:
            # in case the state is not present in the MUTE_MAP (i.e. not mutable)
            self.log_exception(self.DEBUG, e, "trying to send mute command")

    async def async_test(self, /):
        await self.async_request_payload({mc.KEY_STATUS: 23})


class ms100(SensorSubDevice, SensorParser):

    class SensorAdjustNumber(NumberParser):

        init_ns = mn_h.Appliance_Hub_Sensor_Adjust

        init_device_scale = 10

        @override
        async def async_request_value(self, device_value, /):
            # the SET command on NS_APPLIANCE_HUB_SENSOR_ADJUST works by applying
            # the issued value as a 'delta' to the current configured value i.e.
            # 'new adjust value' = 'current adjust value' + 'issued adjust value'
            # Since the native HA interface async_set_native_value wants to set
            # the 'new adjust value' we have to issue the difference against the
            # currently configured one
            await self.async_request_payload(
                {self.key_value: device_value - self.device_value}
            )
            self.update_device_value(device_value)

    class AdjustTemperatureNumber(SensorAdjustNumber):

        init_entity_key = "config_adjust_temperature"
        init_key_value = mc.KEY_TEMPERATURE
        _attr_device_class = NumberParser.DeviceClass.TEMPERATURE
        _attr_name = "Adjust temperature"
        _attr_native_min_value = -5
        _attr_native_max_value = 5
        _attr_native_step = 0.1

    class AdjustHumidityNumber(SensorAdjustNumber):

        init_entity_key = "config_adjust_humidity"
        init_key_value = mc.KEY_HUMIDITY
        _attr_device_class = NumberParser.DeviceClass.HUMIDITY
        _attr_name = "Adjust humidity"
        _attr_native_min_value = -20
        _attr_native_max_value = 20
        _attr_native_step = 1

    NS_HUB = (
        mn_h.Appliance_Hub_Sensor_Adjust,
        mn_h.Appliance_Hub_Sensor_Latest,
        *SensorSubDevice.NS_HUB,
    )

    init_ns = mn_h.Appliance_Hub_Sensor_TempHum

    __slots__ = ("sensor_humidity",)

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SensorSubDevice.__init__(
            self, subid, hub, key_digest, model, **SensorParser.TEMPERATURE_ARGS
        )
        self.sensor_humidity = SensorParser(subid, hub, **SensorParser.HUMIDITY_ARGS)

    def shutdown(self):
        SensorSubDevice.shutdown(self)
        del self.sensor_humidity

    @override
    def _parse(self, payload: "mt.hub.Sensor_TempHum | mt.hub._ms100", /):
        self._update_sensors(
            payload[mc.KEY_LATESTTEMPERATURE], payload[mc.KEY_LATESTHUMIDITY]
        )

    @override
    def _parse_all(self, payload: "mt.hub.Sensor_All_ms100", /):
        self._parse_online(payload[mc.KEY_ONLINE])
        if self.available:
            self._update_sensors(
                payload[mc.KEY_TEMPERATURE][mc.KEY_LATEST],
                payload[mc.KEY_HUMIDITY][mc.KEY_LATEST],
            )

    def _parse_adjust(self, payload: "mt.hub.Sensor_Adjust"):
        device = self.parent
        self.handlers[mn_h.Appliance_Hub_Sensor_Adjust].swap_parsers(
            self,
            *device.add_entities(
                [
                    ms100.AdjustTemperatureNumber(
                        self.channel,
                        device,
                        device_value=payload[mc.KEY_TEMPERATURE],
                    ),
                    ms100.AdjustHumidityNumber(
                        self.channel,
                        device,
                        device_value=payload[mc.KEY_HUMIDITY],
                    ),
                ]
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
        self.update_device_value(temperature)
        self.sensor_humidity.update_device_value(humidity)

    def _update_sensors_adjust(self, temperature: int, humidity: int):
        # when a temp/hum reading changes we're smartly requesting
        # the adjust sooner than scheduled in case the change
        # was due to an adjustment. This method is dynamically installed
        # by _parse_adjust when we have confirtmation that ns_adjust is
        # delivering for this device.
        _poll_adjust = bool(self.update_device_value(temperature))
        _poll_adjust |= bool(self.sensor_humidity.update_device_value(humidity))
        if _poll_adjust:

            handler = self.parent.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust]
            if handler.last_poll_epoch < (self.parent.last_rx_epoch - 30):
                handler.next_poll_epoch = 0.0


class ms130(ms100):

    NS_HUB = (mn.Appliance_Config_DeviceCfg, *ms100.NS_HUB)
    init_device_scale = 100

    __slots__ = ("sensor_light",)

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        ms100.__init__(self, subid, hub, key_digest, model)
        self.sensor_light = SensorParser.Light(subid, hub)
        hub.get_handler(mn.Appliance_Control_Sensor_LatestX).register_parser(
            self
        ).update(
            {"channel": 0, "data": ["light", "temp", "humi"]},
        )

    def shutdown(self):
        ms100.shutdown(self)
        del self.sensor_light

    @override
    def _parse(self, payload: "mt.hub._tempHumi", /):
        self._update_sensors(payload[mc.KEY_TEMP], payload[mc.KEY_HUMI])

    def _parse_deviceCfg(self, payload: "mt.hub.SubIdPayload", /):
        """TODO: implement entities
        {
            "calibrateCfg": {
            "temp": 0,
            "humi": 0
            },
            "timeCfg": {
            "am": 2
            },
            "ms130Cfg": {
            "bl": {
                "bri": 2,
                "lv": 4,
                "sleep": 10
            }
            },
            "channel": 0,
            "subId": "1A00694ACBC7",
            "unitCfg": {
            "tempUnit": 1  # 1 °C - 2 °F
            }
        }
        """
        pass

    def _parse_latestx(self, payload: "mt.sensor.LatestX_C", /):
        """parser for Appliance.Control.Sensor.LatestX:
        {
            "latest": [
                {
                    "data": {
                        "light": [{"value": 220, "timestamp": 1722349685}],
                        "temp": [{"value": 2134, "timestamp": 1722349685}],
                        "humi": [{"value": 670, "timestamp": 1722349685}],
                    },
                    "channel": 0,
                    "subId": "1A00694ACBC7",
                }
            ]
        }
        """
        p_data = payload[mc.KEY_DATA]
        entity: SensorParser
        for key, entity in {
            mc.KEY_TEMP: self,
            mc.KEY_HUMI: self.sensor_humidity,
            mc.KEY_LIGHT: self.sensor_light,
        }.items():
            try:
                entity.update_device_value(p_data[key][0][mc.KEY_VALUE])
            except:
                pass


class ms200(SensorSubDevice, BinarySensorParser):
    init_entity_key = BinarySensorParser.DeviceClass.WINDOW
    init_ns = mn_h.Appliance_Hub_Sensor_DoorWindow
    init_key_value = mc.KEY_STATUS
    _attr_device_class = BinarySensorParser.DeviceClass.WINDOW


class ms400(SensorSubDevice, BinarySensorParser):
    init_entity_key = mc.KEY_WATERLEAK
    init_ns = mn_h.Appliance_Hub_Sensor_WaterLeak
    init_key_value = mc.KEY_LATESTWATERLEAK
    _attr_device_class = BinarySensorParser.DeviceClass.SAFETY
