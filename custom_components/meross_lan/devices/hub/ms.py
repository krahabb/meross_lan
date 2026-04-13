from typing import TYPE_CHECKING, override

from . import SubDevice, mc, mn, mn_h
from ...binary_sensor import BinarySensorEntity, BinarySensorParser
from ...button import Button
from ...number import NumberParser
from ...sensor import EnumParser, EnumSensorEntity, SensorEntity, SensorParser

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
    if TYPE_CHECKING:
        STATUS_MAP: Final
        MUTE_MAP: Final
        STATUS_ALARM: Final[set[int]]
        STATUS_ERROR: Final[set[int]]
        STATUS_MUTED: Final[set[int]]

        binary_sensor_alarm: BinarySensorEntity
        binary_sensor_error: BinarySensorEntity
        binary_sensor_muted: BinarySensorEntity
        sensor_interConn: EnumSensorEntity

    init_ns = mn_h.Appliance_Hub_Sensor_Smoke
    init_key_value = EnumParser.SimpleKeyValue(mc.KEY_STATUS)
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

    ENTITY_DEFS: "dict[str, type[Entity]]" = {
        "binary_sensor_alarm": BinarySensorEntity.ENTITY_DEF(
            entity_key=mc.KEY_ALARM, device_class=BinarySensorEntity.DeviceClass.SAFETY
        ),
        "binary_sensor_error": BinarySensorEntity.ENTITY_DEF(
            entity_key=mc.KEY_ERROR, device_class=BinarySensorEntity.DeviceClass.PROBLEM
        ),
        "binary_sensor_muted": BinarySensorEntity.ENTITY_DEF(entity_key="muted"),
        "sensor_interConn": EnumParser.ENTITY_DEF(entity_key=mc.KEY_INTERCONN),
    }
    __slots__ = ENTITY_DEFS.keys()

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SensorSubDevice.__init__(self, subid, hub, key_digest, model)
        self.unique_id = f"{hub.id}_{subid}_status"  # LEGACY unique_id scheme
        for key, entity_def in self.__class__.ENTITY_DEFS.items():
            setattr(self, key, entity_def(self))
        Button(self, async_press=self.async_mute, name="Mute")
        Button(self, async_press=self.async_test, name="Test")

    def _parse(self, payload: "mt.hub._gs559 | mt.hub.Sensor_Smoke", /):
        # This (being the default fall-back parser) will parse  *.Sensor.All, *Sensor.Smoke
        # and the 'digest' payload since they have the same structure.
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
        for key in self.__class__.ENTITY_DEFS.keys():
            delattr(self, key)

    async def async_mute(self, /):
        await self.async_request_value(self.MUTE_MAP.get(self.device_value, 170))

    async def async_test(self, /):
        await self.async_request_value(23)


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
                self.key_value(device_value - self.device_value)
            )
            self.update_device_value(device_value)

    class AdjustTemperatureNumber(SensorAdjustNumber):

        init_entity_key = "config_adjust_temperature"
        init_key_value = NumberParser.SimpleKeyValue(mc.KEY_TEMPERATURE)
        _attr_device_class = NumberParser.DeviceClass.TEMPERATURE
        _attr_name = "Adjust temperature"
        _attr_native_min_value = -5
        _attr_native_max_value = 5
        _attr_native_step = 0.1

    class AdjustHumidityNumber(SensorAdjustNumber):

        init_entity_key = "config_adjust_humidity"
        init_key_value = NumberParser.SimpleKeyValue(mc.KEY_HUMIDITY)
        _attr_device_class = NumberParser.DeviceClass.HUMIDITY
        _attr_name = "Adjust humidity"
        _attr_native_min_value = -20
        _attr_native_max_value = 20
        _attr_native_step = 1

    NS_HUB = (
        mn_h.Appliance_Hub_Sensor_TempHum,
        mn_h.Appliance_Hub_Sensor_Adjust,
        mn_h.Appliance_Hub_Sensor_Latest,
    )

    init_device_scale = 10
    _attr_device_class = SensorEntity.DeviceClass.TEMPERATURE
    _attr_suggested_display_precision = 1

    __slots__ = ("sensor_humidity",)

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        SensorSubDevice.__init__(self, subid, hub, key_digest, model)
        self.unique_id = f"{hub.id}_{subid}_temperature"  # LEGACY unique_id scheme
        self.sensor_humidity = SensorParser(self, **SensorParser.HUMIDITY_ARGS)

    def shutdown(self):
        SensorSubDevice.shutdown(self)
        del self.sensor_humidity

    @override
    def _parse(self, payload: "mt.hub._ms100 | mt.hub.Sensor_TempHum", /):
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
        device = self.parent
        device.ns_handlers[mn_h.Appliance_Hub_Sensor_Adjust].swap_parsers(
            self,
            *(
                device.add_entity(
                    entity_class(
                        self, device_value=entity_class.init_key_value[payload]
                    )
                )
                for entity_class in (
                    ms100.AdjustTemperatureNumber,
                    ms100.AdjustHumidityNumber,
                )
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

    NS_HUB = (
        mn.Appliance_Control_Sensor_LatestX,
        mn.Appliance_Config_DeviceCfg,
    )
    init_device_scale = 100

    __slots__ = ("sensor_light",)

    def __init__(self, subid: str, hub: "Hub", key_digest: str, model: str, /):
        ms100.__init__(self, subid, hub, key_digest, model)
        # The light sensor could be better indexed by subid instead, but it is a sibling of a
        # simple 'id' entity (the ms130 itself) and it is more natural to mantain the sibling
        # relationship by using the same index. The sensor will anyway not use the index attribute
        # for anything else, since the ns parsing is done here in the SubDevice instance.
        self.sensor_light = SensorParser(self, **(SensorParser.LIGHT_ARGS))
        # This is a slight patch because this ns is rather non-standard
        handler_latestx = hub.ns_handlers[mn.Appliance_Control_Sensor_LatestX]
        # the latest payload was added in SubDevice init because of NS_HUB registration
        handler_latestx.polling_request_payload.append(
            handler_latestx.polling_request_payload.pop()
            | {mc.KEY_DATA: [mc.KEY_TEMP, mc.KEY_HUMI, mc.KEY_LIGHT]}
        )

    def shutdown(self):
        ms100.shutdown(self)
        del self.sensor_light

    @override
    def _parse(self, payload: "mt.hub._ms130", /):
        # Parses the 'digest' payload.
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
    NS_HUB = (mn_h.Appliance_Hub_Sensor_DoorWindow,)
    init_key_value = BinarySensorParser.SimpleKeyValue(mc.KEY_STATUS)
    _attr_device_class = BinarySensorParser.DeviceClass.WINDOW

    @property
    def unique_id(self) -> str | None:
        return f"{self.parent.id}_{self.id}_window"

    @unique_id.setter
    def unique_id(self, value):
        pass


class ms400(SensorSubDevice, BinarySensorParser):
    NS_HUB = (mn_h.Appliance_Hub_Sensor_WaterLeak,)
    init_key_value = BinarySensorParser.SimpleKeyValue(mc.KEY_LATESTWATERLEAK)
    _attr_device_class = BinarySensorParser.DeviceClass.SAFETY

    @property
    def unique_id(self) -> str | None:
        return f"{self.parent.id}_{self.id}_waterleak"

    @unique_id.setter
    def unique_id(self, value):
        pass
