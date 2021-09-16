from __future__ import annotations

from time import time
from typing import Callable, Dict

from homeassistant.const import (
    DEVICE_CLASS_BATTERY,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_HUMIDITY,
)
from homeassistant.components.binary_sensor import DEVICE_CLASS_WINDOW

from .merossclient import KeyType, const as mc  # mEROSS cONST
from .meross_device import MerossDevice, Protocol
from .sensor import PLATFORM_SENSOR, MerossLanHubSensor
from .climate import PLATFORM_CLIMATE, Mts100Climate
from .binary_sensor import PLATFORM_BINARY_SENSOR, MerossLanHubBinarySensor
from .helpers import LOGGER
from .const import (
    PARAM_HEARTBEAT_PERIOD,
    PARAM_HUBBATTERY_UPDATE_PERIOD,
    PARAM_HUBSENSOR_UPDATE_PERIOD,
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

def _get_subdevice_type(p_digest: dict) -> str:
    """
    parses the subdevice payload in 'digest' to look for a well-known type
    or extract the type itself:
    """
    for p_key, p_value in p_digest.items():
        if isinstance(p_value, dict):
            return p_key
    return None


def _get_temp_normal(value: int | None, default) -> float | None:
    if isinstance(value, int):
        return value / 10
    return default



class MerossDeviceHub(MerossDevice):

    def __init__(self, api, descriptor, entry) -> None:
        super().__init__(api, descriptor, entry)
        self.subdevices: Dict[any, MerossSubDevice] = {}
        self._lastupdate_battery = 0
        self._lastupdate_sensor = 0
        self._lastupdate_mts100 = 0
        """
            invoke platform(s) async_setup_entry
            in order to be able to eventually add entities when they 'pop up'
            in the hub (see also self.async_add_sensors)
        """
        self.platforms[PLATFORM_SENSOR] = None
        self.platforms[PLATFORM_BINARY_SENSOR] = None
        self.platforms[PLATFORM_CLIMATE] = None

        self.polling_dictionary.append(mc.NS_APPLIANCE_HUB_SENSOR_ALL)
        self.polling_dictionary.append(mc.NS_APPLIANCE_HUB_MTS100_ALL)

        try:
            # we expect a well structured digest here since
            # we're sure 'hub' key is there by __init__ device factory
            p_digest = self.descriptor.digest
            p_hub = p_digest[mc.KEY_HUB]
            p_subdevices = p_hub[mc.KEY_SUBDEVICE]
            for p_subdevice in p_subdevices:
                type = _get_subdevice_type(p_subdevice)
                if type is None:
                    continue # bugged/incomplete configuration payload..wait for some good updates
                deviceclass = WELL_KNOWN_TYPE_MAP.get(type)
                if deviceclass is None:
                    # build something anyway...
                    MerossSubDevice(self, p_subdevice, type)
                else:
                    deviceclass(self, p_subdevice)

        except Exception as e:
            LOGGER.warning("MerossDeviceHub(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_HUB_SENSOR_ALL:
            self._lastupdate_sensor = self.lastupdate
            self._subdevice_parse(payload, mc.KEY_ALL)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_SENSOR_TEMPHUM:
            self._lastupdate_sensor = self.lastupdate
            self._subdevice_parse(payload, mc.KEY_TEMPHUM)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_MTS100_ALL:
            self._lastupdate_mts100 = self.lastupdate
            self._subdevice_parse(payload, mc.KEY_ALL)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_MTS100_MODE:
            self._subdevice_parse(payload, mc.KEY_MODE)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_MTS100_TEMPERATURE:
            self._subdevice_parse(payload, mc.KEY_TEMPERATURE)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_TOGGLEX:
            self._subdevice_parse(payload, mc.KEY_TOGGLEX)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_BATTERY:
            self._lastupdate_battery = self.lastupdate
            self._subdevice_parse(payload, mc.KEY_BATTERY)
            return True

        if namespace == mc.NS_APPLIANCE_HUB_ONLINE:
            self._subdevice_parse(payload, mc.KEY_ONLINE)
            return True

        if namespace == mc.NS_APPLIANCE_DIGEST_HUB:
            self._parse_hub(payload.get(mc.KEY_HUB))
            return True

        return False


    def _subdevice_parse(self, payload: dict, key: str) -> None:
        p_subdevices = payload.get(key)
        if isinstance(p_subdevices, list):
            for p_subdevice in p_subdevices:
                p_id = p_subdevice.get(mc.KEY_ID)
                subdevice = self.subdevices.get(p_id)
                if subdevice is None:# force a rescan since we discovered a new subdevice
                    self.request_get(mc.NS_APPLIANCE_SYSTEM_ALL)
                else:
                    method = getattr(subdevice, f"_parse_{key}", None)
                    if method is not None:
                        method(p_subdevice)


    def _parse_hub(self, p_hub: dict) -> None:
        p_subdevices = p_hub.get(mc.KEY_SUBDEVICE)
        if isinstance(p_subdevices, list):
            for p_digest in p_subdevices:
                p_id = p_digest.get(mc.KEY_ID)
                subdevice = self.subdevices.get(p_id)
                if subdevice is None:
                    self.needsave = True
                    type = _get_subdevice_type(p_digest)
                    if type is None:
                        # the hub could report incomplete info anytime so beware!
                        continue
                    deviceclass = WELL_KNOWN_TYPE_MAP.get(type)
                    if deviceclass is None:
                        # build something anyway...
                        subdevice = MerossSubDevice(self, p_digest, type)
                    else:
                        subdevice = deviceclass(self, p_digest)
                subdevice.update_digest(p_digest)


    def _request_updates(self, epoch, namespace):
        super()._request_updates(epoch, namespace)
        """
        we just ask for updates when something pops online (_lastupdate_sensor == 0)
        relying on push (over MQTT) or base polling updates (only HTTP) for any other changes
        """
        if self.curr_protocol == Protocol.MQTT:
            if self._lastupdate_sensor == 0:
                self.request_get(mc.NS_APPLIANCE_HUB_SENSOR_ALL)
            if self._lastupdate_mts100 == 0:
                self.request_get(mc.NS_APPLIANCE_HUB_MTS100_ALL)

        if ((epoch - self._lastupdate_battery) >= PARAM_HUBBATTERY_UPDATE_PERIOD):
            self.request_get(mc.NS_APPLIANCE_HUB_BATTERY)



class MerossSubDevice:

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, type: str) -> None:
        self.hub = hub
        self.type = type
        self.id = p_digest.get(mc.KEY_ID)
        self.p_digest = p_digest
        self._online = False
        hub.subdevices[self.id] = self
        self.sensor_battery = MerossLanHubSensor(self, DEVICE_CLASS_BATTERY)


    @property
    def online(self) -> bool:
        return self._online


    def _setonline(self, status) -> None:
        if status == mc.STATUS_ONLINE:
            if self._online is False:
                self._online = True
                """
                here we should request updates for all entities but
                there could be some 'light' race conditions
                since when the device (hub) comes online it requests itself
                a full update and this could be the case.
                If instead this online status change is due to the single
                subdevice coming online then we'll just wait for the next
                polling cycle by setting the battery update trigger..
                sensors are instead being updated in this call stack
                """
                self.hub._lastupdate_battery = 0
                self.hub._lastupdate_sensor = 0
                self.hub._lastupdate_mts100 = 0
        else:
            if self._online is True:
                self._online = False
                for entity in self.hub.entities.values():
                    # not every entity in hub is a 'subdevice' entity
                    if getattr(entity, "subdevice", None) is self:
                        entity._set_unavailable()


    def update_digest(self, p_digest: dict) -> None:
        self.p_digest = p_digest
        self._setonline(p_digest.get(mc.KEY_STATUS))


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
        self._setonline(p_all.get(mc.KEY_ONLINE, {}).get(mc.KEY_STATUS))

        if self._online:
            for p_key, p_value in p_all.items():
                if isinstance(p_value, dict):
                    p_latest = p_value.get(mc.KEY_LATEST)
                    if isinstance(p_latest, int):
                        sensorattr = f"sensor_{p_key}"
                        sensor:MerossLanHubSensor = getattr(self, sensorattr, None)
                        if not sensor:
                            sensor = MerossLanHubSensor(self, p_key)
                            setattr(self, sensorattr, sensor)
                        sensor._set_state(p_latest / 10)


    def _parse_battery(self, p_battery: dict) -> None:
        if self._online:
            self.sensor_battery._set_state(p_battery.get(mc.KEY_VALUE))


    def _parse_online(self, p_online: dict) -> None:
        self._setonline(p_online.get(mc.KEY_STATUS))



class MS100SubDevice(MerossSubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict) -> None:
        super().__init__(hub, p_digest, mc.TYPE_MS100)
        self.sensor_temperature = MerossLanHubSensor(self, DEVICE_CLASS_TEMPERATURE)
        self.sensor_humidity = MerossLanHubSensor(self, DEVICE_CLASS_HUMIDITY)


    def update_digest(self, p_digest: dict) -> None:
        super().update_digest(p_digest)
        if self._online:
            p_ms100 = p_digest.get(mc.TYPE_MS100)
            if p_ms100 is not None:
                # beware! it happens some keys are missing sometimes!!!
                value = p_ms100.get(mc.KEY_LATESTTEMPERATURE)
                if isinstance(value, int):
                    self.sensor_temperature._set_state(value / 10)
                value = p_ms100.get(mc.KEY_LATESTHUMIDITY)
                if isinstance(value, int):
                    self.sensor_humidity._set_state(value / 10)


    def _parse_tempHum(self, p_temphum: dict) -> None:
        value = p_temphum.get(mc.KEY_LATESTTEMPERATURE)
        if isinstance(value, int):
            self.sensor_temperature._set_state(value / 10)
        value = p_temphum.get(mc.KEY_LATESTHUMIDITY)
        if isinstance(value, int):
            self.sensor_humidity._set_state(value / 10)


WELL_KNOWN_TYPE_MAP[mc.TYPE_MS100] = MS100SubDevice



class MTS100SubDevice(MerossSubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict, type: str = mc.TYPE_MTS100) -> None:
        super().__init__(hub, p_digest, type)
        self.climate = Mts100Climate(self)
        self.binary_sensor_window = MerossLanHubBinarySensor(self, DEVICE_CLASS_WINDOW)
        self.sensor_temperature = MerossLanHubSensor(self, DEVICE_CLASS_TEMPERATURE)


    def _parse_all(self, p_all: dict) -> None:
        super()._parse_all(p_all)

        climate = self.climate
        p_mode = p_all.get(mc.KEY_MODE)
        if isinstance(p_mode, dict):
            climate._mts100_mode = p_mode.get(mc.KEY_STATE)

        p_temperature = p_all.get(mc.KEY_TEMPERATURE)
        if isinstance(p_temperature, dict):
            climate._current_temperature = _get_temp_normal(p_temperature.get(mc.KEY_ROOM), climate._current_temperature)
            climate._target_temperature = _get_temp_normal(p_temperature.get(mc.KEY_CURRENTSET), climate._target_temperature)
            climate._min_temp = _get_temp_normal(p_temperature.get(mc.KEY_MIN), climate._min_temp)
            climate._max_temp = _get_temp_normal(p_temperature.get(mc.KEY_MAX), climate._max_temp)
            climate._mts100_heating = p_temperature.get(mc.KEY_HEATING)

            p_openwindow = p_temperature.get(mc.KEY_OPENWINDOW)
            if p_openwindow is not None:
                self.binary_sensor_window._set_onoff(p_openwindow)

            self.sensor_temperature._set_state(climate._current_temperature)

        p_togglex = p_all.get(mc.KEY_TOGGLEX)
        if isinstance(p_togglex, dict):
            climate._mts100_onoff = p_togglex.get(mc.KEY_ONOFF)

        climate.update_modes()


    def _parse_mode(self, p_mode: dict) -> None:
        climate = self.climate
        climate._mts100_mode = p_mode.get(mc.KEY_STATE)
        climate.update_modes()


    def _parse_temperature(self, p_temperature: dict) -> None:
        climate = self.climate
        climate._current_temperature = _get_temp_normal(p_temperature.get(mc.KEY_ROOM), climate._current_temperature)
        climate._target_temperature = _get_temp_normal(p_temperature.get(mc.KEY_CURRENTSET), climate._target_temperature)
        climate._min_temp = _get_temp_normal(p_temperature.get(mc.KEY_MIN), climate._min_temp)
        climate._max_temp = _get_temp_normal(p_temperature.get(mc.KEY_MAX), climate._max_temp)
        climate._mts100_heating = p_temperature.get(mc.KEY_HEATING, climate._mts100_heating)
        climate.update_modes()
        self.sensor_temperature._set_state(climate._current_temperature)


    def _parse_togglex(self, p_togglex: dict) -> None:
        climate = self.climate
        climate._mts100_onoff = p_togglex.get(mc.KEY_ONOFF)
        climate.update_modes()


WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100] = MTS100SubDevice



class MTS100V3SubDevice(MTS100SubDevice):

    def __init__(self, hub: MerossDeviceHub, p_digest: dict) -> None:
        super().__init__(hub, p_digest, mc.TYPE_MTS100V3)

WELL_KNOWN_TYPE_MAP[mc.TYPE_MTS100V3] = MTS100V3SubDevice