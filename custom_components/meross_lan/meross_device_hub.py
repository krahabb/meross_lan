from __future__ import annotations
import logging

from typing import Callable, Dict

from homeassistant.const import (
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_HUMIDITY,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import device_registry

from .merossclient import (
    const as mc, # mEROSS cONST
    MerossDeviceDescriptor,
    get_productnameuuid
)
from .meross_device import MerossDevice
from .sensor import PLATFORM_SENSOR, MLSensor
from .climate import PLATFORM_CLIMATE
from .binary_sensor import PLATFORM_BINARY_SENSOR, MLBinarySensor, DEVICE_CLASS_WINDOW
from .number import PLATFORM_NUMBER, MLHubAdjustNumber
from .helpers import LOGGER
from .const import (
    DOMAIN,
    PARAM_HEARTBEAT_PERIOD,
    PARAM_HUBBATTERY_UPDATE_PERIOD,
)


WELL_KNOWN_TYPE_MAP: Dict[str, Callable] = dict({
})
"""
{
    mc.TYPE_MS100: MS100SubDevice,
    mc.TYPE_MTS100: MTS100SubDevice,
    ...
}
"""

#REMOVE
TRICK = False

class MerossDeviceHub(MerossDevice):

    _lastupdate_battery = 0
    _lastupdate_sensor = None
    _lastupdate_mts100 = None


    def __init__(self, api, descriptor: MerossDeviceDescriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        self.subdevices: Dict[any, MerossSubDevice] = {}
        """
            invoke platform(s) async_setup_entry
            in order to be able to eventually add entities when they 'pop up'
            in the hub (see also self.async_add_sensors)
        """
        self.platforms[PLATFORM_SENSOR] = None
        self.platforms[PLATFORM_BINARY_SENSOR] = None
        self.platforms[PLATFORM_CLIMATE] = None
        self.platforms[PLATFORM_NUMBER] = None

        try:
            # we expect a well structured digest here since
            # we're sure 'hub' key is there by __init__ device factory
            for p_subdevice in descriptor.digest[mc.KEY_HUB][mc.KEY_SUBDEVICE]:
                self._subdevice_build(p_subdevice)

            #REMOVE
            global TRICK
            if TRICK:
                TRICK = False
                MS100SubDevice(
                    self,
                    {"id": "120027D281CF", "status": 1, "onoff": 0, "lastActiveTime": 1638019438, "ms100": {"latestTime": 1638019438, "latestTemperature": 224, "latestHumidity": 460, "voltage": 2766}}
                )

        except Exception as e:
            LOGGER.warning("MerossDeviceHub(%s) init exception:(%s)", self.device_id, str(e))


    def _handle_Appliance_Hub_Sensor_All(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._lastupdate_sensor = self.lastupdate
        self._subdevice_parse(payload, mc.KEY_ALL)
        self.request_get(mc.NS_APPLIANCE_HUB_SENSOR_ADJUST)


    def _handle_Appliance_Hub_Sensor_TempHum(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._lastupdate_sensor = self.lastupdate
        self._subdevice_parse(payload, mc.KEY_TEMPHUM)


    def _handle_Appliance_Hub_Sensor_Adjust(self,
    namespace: str, method: str, payload: dict, header: dict):
        if method == mc.METHOD_SETACK:
            self.request_get(mc.NS_APPLIANCE_HUB_SENSOR_ADJUST)
        else:
            self._subdevice_parse(payload, mc.KEY_ADJUST)


    def _handle_Appliance_Hub_Mts100_All(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._lastupdate_mts100 = self.lastupdate
        self._subdevice_parse(payload, mc.KEY_ALL)
        self.request_get(mc.NS_APPLIANCE_HUB_MTS100_ADJUST)


    def _handle_Appliance_Hub_Mts100_Mode(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._subdevice_parse(payload, mc.KEY_MODE)


    def _handle_Appliance_Hub_Mts100_Temperature(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._subdevice_parse(payload, mc.KEY_TEMPERATURE)


    def _handle_Appliance_Hub_Mts100_Adjust(self,
    namespace: str, method: str, payload: dict, header: dict):
        if method == mc.METHOD_SETACK:
            self.request_get(mc.NS_APPLIANCE_HUB_MTS100_ADJUST)
        else:
            self._subdevice_parse(payload, mc.KEY_ADJUST)

    def _handle_Appliance_Hub_ToggleX(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._subdevice_parse(payload, mc.KEY_TOGGLEX)


    def _handle_Appliance_Hub_Battery(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._lastupdate_battery = self.lastupdate
        self._subdevice_parse(payload, mc.KEY_BATTERY)


    def _handle_Appliance_Hub_Online(self,
    namespace: str, method: str, payload: dict, header: dict):
        self._subdevice_parse(payload, mc.KEY_ONLINE)


    def _handle_Appliance_Digest_Hub(self,
            namespace: str, method: str, payload: dict, header: dict):
        self._parse_hub(payload.get(mc.KEY_HUB))


    def _subdevice_build(self, p_subdevice: dict) -> MerossSubDevice:
        """
        parses the subdevice payload in 'digest' to look for a well-known type
        and builds accordingly
        """
        _type = None
        for p_key, p_value in p_subdevice.items():
            if isinstance(p_value, dict):
                _type = p_key
                break
        else:
            # the hub could report incomplete info anytime so beware.
            # this is true when subdevice is offline and hub has no recent info
            # we'll check our device registry for luck
            try:
                hassdevice = device_registry.async_get(self.api.hass).async_get_device(
                    identifiers = {(DOMAIN, p_subdevice.get(mc.KEY_ID))}
                )
                if hassdevice is None:
                    return None
                _type = hassdevice.model
            except:
                return None
        deviceclass = WELL_KNOWN_TYPE_MAP.get(_type)
        if deviceclass is None:
            # build something anyway...
            return MerossSubDevice(self, p_subdevice, _type)
        return deviceclass(self, p_subdevice)


    def _subdevice_parse(self, payload: dict, key: str) -> None:
        if isinstance(p_subdevices := payload.get(key), list):
            for p_subdevice in p_subdevices:
                p_id = p_subdevice.get(mc.KEY_ID)
                subdevice = self.subdevices.get(p_id)
                if subdevice is None:
                    # force a rescan since we discovered a new subdevice
                    # only if it appears this device is online else it
                    # would be a waste since we wouldnt have enough info
                    # to correctly build that
                    if p_subdevice.get(mc.KEY_ONLINE, {}).get(mc.KEY_STATUS) == mc.STATUS_ONLINE:
                        self.request_get(mc.NS_APPLIANCE_SYSTEM_ALL)
                else:
                    method = getattr(subdevice, f"_parse_{key}", None)
                    if method is not None:
                        method(p_subdevice)


    def _parse_hub(self, p_hub: dict) -> None:
        """
        This is usually called inside _parse_all as part of the digest parsing
        Here we'll check the fresh subdevice list against the actual one and
        eventually manage newly added subdevices or removed ones #119
        telling the caller to persist the changed configuration (self.needsave)
        """
        if isinstance(p_subdevices := p_hub.get(mc.KEY_SUBDEVICE), list):
            subdevices_actual: set = set(self.subdevices.keys())
            for p_digest in p_subdevices:
                p_id = p_digest.get(mc.KEY_ID)
                subdevice = self.subdevices.get(p_id)
                if subdevice is None:
                    subdevice = self._subdevice_build(p_digest)
                    if subdevice is None:
                        continue
                    self.needsave = True
                else:
                    subdevices_actual.remove(p_id)
                subdevice.update_digest(p_digest)

            if len(subdevices_actual):
                # now we're left with non-existent (removed) subdevices
                self.needsave = True
                for p_id in subdevices_actual:
                    subdevice = self.subdevices.pop(p_id)
                    self.log(logging.WARNING, 0, "removing subdevice %s(%s) - configuration will be reloaded in 15 sec", subdevice.type, p_id)
                """
                before reloading we have to be sure configentry data were persisted
                so we'll wait a bit..
                also, we're not registering an unsub and we're not checking
                for redundant invocations (playing a bit unsafe that is)
                """
                hass = self.api.hass
                async def setup_again(*_) -> None:
                    await hass.config_entries.async_reload(self.entry_id)
                async_call_later(hass, 15, setup_again)


    def _request_updates(self, epoch, namespace):
        super()._request_updates(epoch, namespace)
        """
        we just ask for updates when something pops online (_lastupdate_xxxx == 0)
        relying on push (over MQTT) or base polling updates (only HTTP) for any other changes
        if _lastupdate_xxxx is None then it means that device class is not present in the hub
        and we totally skip the request. This is especially true since I've discovered hubs
        don't expose the full set of namespaces until a real subdevice type is binded.
        If this is the case we would ask a namespace which is not supported at the moment
        (see #167)
        """
        if self._lastupdate_sensor is not None:
            if (self._lastupdate_sensor == 0) or ((epoch - self.lastmqtt) > PARAM_HEARTBEAT_PERIOD):
                self.request_get(mc.NS_APPLIANCE_HUB_SENSOR_ALL)
        if self._lastupdate_mts100 is not None:
            if (self._lastupdate_mts100 == 0) or ((epoch - self.lastmqtt) > PARAM_HEARTBEAT_PERIOD):
                self.request_get(mc.NS_APPLIANCE_HUB_MTS100_ALL)
        if ((epoch - self._lastupdate_battery) >= PARAM_HUBBATTERY_UPDATE_PERIOD):
            self.request_get(mc.NS_APPLIANCE_HUB_BATTERY)



class MerossSubDevice:

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, _type: str) -> None:
        self.hub = hub
        self.type = _type
        self.id = p_digest.get(mc.KEY_ID)
        self.p_digest = p_digest
        self._online = False
        hub.subdevices[self.id] = self
        self.sensor_battery = MLSensor.build_for_subdevice(self, DEVICE_CLASS_BATTERY)


    @property
    def online(self) -> bool:
        return self._online


    @property
    def name(self) -> str:
        return get_productnameuuid(self.type, self.id)


    def _setonline(self) -> None:
        """
        here we should request updates for all entities but
        there could be some 'light' race conditions
        since when the device (hub) comes online it requests itself
        a full update and this could be the case.
        If instead this online status change is due to the single
        subdevice coming online then we'll just wait for the next
        polling cycle by setting the battery update trigger..
        """
        self.hub._lastupdate_battery = 0


    def update_digest(self, p_digest: dict) -> None:
        self.p_digest = p_digest
        self._parse_online(p_digest)


    def _parse_all(self, p_all: dict) -> None:
        """
        Generally speaking this payload has a couple of well-known keys
        plus a set of sensor values like (MS100 example):
        {
            "id": "..."
            "online: "..."
            "temperature": {
                "latest": value
                ...
            }
            "humidity": {
                "latest": value
                ...
            }
        }
        so we just extract generic sensors where we find 'latest'
        Luckily enough the key names in Meross will behave consistently in HA
        at least for 'temperature' and 'humidity' (so far..) also, we divide
        the value by 10 since that's a correct eurhystic for them (so far..)
        """
        self.p_subdevice = p_all # warning: digest here could be a generic 'sensor' payload
        self._parse_online(p_all.get(mc.KEY_ONLINE, {}))

        if self._online:
            for p_key, p_value in p_all.items():
                if isinstance(p_value, dict):
                    p_latest = p_value.get(mc.KEY_LATEST)
                    if isinstance(p_latest, int):
                        sensorattr = f"sensor_{p_key}"
                        sensor:MLSensor = getattr(self, sensorattr, None)
                        if not sensor:
                            sensor = MLSensor.build_for_subdevice(self, p_key)
                            setattr(self, sensorattr, sensor)
                        sensor.update_state(p_latest / 10)


    def _parse_battery(self, p_battery: dict) -> None:
        if self._online:
            self.sensor_battery.update_state(p_battery.get(mc.KEY_VALUE))


    def _parse_online(self, p_online: dict) -> None:
        if mc.KEY_STATUS in p_online:
            if p_online[mc.KEY_STATUS] == mc.STATUS_ONLINE:
                if self._online is False:
                    self._online = True
                    self._setonline()
            else:
                if self._online is True:
                    self._online = False
                    for entity in self.hub.entities.values():
                        # not every entity in hub is a 'subdevice' entity
                        if getattr(entity, "subdevice", None) is self:
                            entity.set_unavailable()



    def _parse_adjust(self, p_adjust: dict) -> None:
        for p_key, p_value in p_adjust.items():
            if p_key == mc.KEY_ID:
                continue
            number:MLHubAdjustNumber
            if (number := getattr(self, f"number_adjust_{p_key}", None)) is not None:
                number.update_value(p_value)


class MS100SubDevice(MerossSubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict) -> None:
        super().__init__(hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = MLSensor.build_for_subdevice(self, DEVICE_CLASS_TEMPERATURE)
        self.sensor_humidity = MLSensor.build_for_subdevice(self, DEVICE_CLASS_HUMIDITY)
        self.number_adjust_temperature = MLHubAdjustNumber(
            self, mc.KEY_TEMPERATURE, mc.NS_APPLIANCE_HUB_SENSOR_ADJUST,
            '', DEVICE_CLASS_TEMPERATURE, -5, 5, 0.1)
        self.number_adjust_humidity = MLHubAdjustNumber(
            self, mc.KEY_HUMIDITY, mc.NS_APPLIANCE_HUB_SENSOR_ADJUST,
            '', DEVICE_CLASS_HUMIDITY, -20, 20, 1)


    def _setonline(self) -> None:
        super()._setonline()
        self.hub._lastupdate_sensor = 0


    def update_digest(self, p_digest: dict) -> None:
        super().update_digest(p_digest)
        if self._online:
            p_ms100 = p_digest.get(mc.TYPE_MS100)
            if isinstance(p_ms100, dict):
                # beware! it happens some keys are missing sometimes!!!
                if isinstance(value := p_ms100.get(mc.KEY_LATESTTEMPERATURE), int):
                    self.sensor_temperature.update_state(value / 10)
                if isinstance(value := p_ms100.get(mc.KEY_LATESTHUMIDITY), int):
                    self.sensor_humidity.update_state(value / 10)


    def _parse_tempHum(self, p_temphum: dict) -> None:
        if isinstance(value := p_temphum.get(mc.KEY_LATESTTEMPERATURE), int):
            self.sensor_temperature.update_state(value / 10)
        if isinstance(value := p_temphum.get(mc.KEY_LATESTHUMIDITY), int):
            self.sensor_humidity.update_state(value / 10)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS100] = MS100SubDevice



class MTS100SubDevice(MerossSubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, _type: str = mc.TYPE_MTS100) -> None:
        super().__init__(hub, p_digest, _type)
        from .devices.mts100 import Mts100Climate
        self.climate = Mts100Climate(self)
        self.binary_sensor_window = MLBinarySensor.build_for_subdevice(
            self, DEVICE_CLASS_WINDOW)
        self.sensor_temperature = MLSensor.build_for_subdevice(
            self, DEVICE_CLASS_TEMPERATURE)
        self.number_adjust_temperature = MLHubAdjustNumber(
            self, mc.KEY_TEMPERATURE, mc.NS_APPLIANCE_HUB_MTS100_ADJUST,
            '', DEVICE_CLASS_TEMPERATURE, -5, 5, 0.1)


    def _setonline(self) -> None:
        super()._setonline()
        self.hub._lastupdate_mts100 = 0


    def _parse_all(self, p_all: dict) -> None:
        super()._parse_all(p_all)

        climate = self.climate

        if isinstance(p_mode := p_all.get(mc.KEY_MODE), dict):
            climate._mts_mode = p_mode.get(mc.KEY_STATE)

        if isinstance(p_togglex := p_all.get(mc.KEY_TOGGLEX), dict):
            climate._mts_onoff = p_togglex.get(mc.KEY_ONOFF)

        if isinstance(p_temperature := p_all.get(mc.KEY_TEMPERATURE), dict):
            self._parse_temperature(p_temperature)
        else:
            climate.update_modes()


    def _parse_mode(self, p_mode: dict) -> None:
        climate = self.climate
        climate._mts_mode = p_mode.get(mc.KEY_STATE)
        climate.update_modes()


    def _parse_temperature(self, p_temperature: dict) -> None:
        climate = self.climate
        if isinstance(_t := p_temperature.get(mc.KEY_ROOM), int):
            climate._attr_current_temperature = _t / 10
            self.sensor_temperature.update_state(climate._attr_current_temperature)
        if isinstance(_t := p_temperature.get(mc.KEY_CURRENTSET), int):
            climate._attr_target_temperature = _t / 10
        if isinstance(_t := p_temperature.get(mc.KEY_MIN), int):
            climate._attr_min_temp = _t / 10
        if isinstance(_t := p_temperature.get(mc.KEY_MAX), int):
            climate._attr_max_temp = _t / 10
        if mc.KEY_HEATING in p_temperature:
            climate._mts_heating = p_temperature[mc.KEY_HEATING]
        climate.update_modes()

        if isinstance(_t := p_temperature.get(mc.KEY_COMFORT), int):
            climate.number_comfort_temperature.update_state(_t / 10)
        if isinstance(_t := p_temperature.get(mc.KEY_ECONOMY), int):
            climate.number_sleep_temperature.update_state(_t / 10)
        if isinstance(_t := p_temperature.get(mc.KEY_AWAY), int):
            climate.number_away_temperature.update_state(_t / 10)

        if mc.KEY_OPENWINDOW in p_temperature:
            self.binary_sensor_window.update_onoff(p_temperature[mc.KEY_OPENWINDOW])


    def _parse_togglex(self, p_togglex: dict) -> None:
        climate = self.climate
        climate._mts_onoff = p_togglex.get(mc.KEY_ONOFF)
        climate.update_modes()


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100] = MTS100SubDevice



class MTS100V3SubDevice(MTS100SubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict) -> None:
        super().__init__(hub, p_digest, mc.TYPE_MTS100V3)

WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100V3] = MTS100V3SubDevice

class MTS150SubDevice(MTS100SubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict) -> None:
        super().__init__(hub, p_digest, mc.TYPE_MTS150)

WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS150] = MTS150SubDevice
