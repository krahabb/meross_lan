from typing import Any, Callable, Dict, List, Optional

from hashlib import md5
from time import time, strftime, localtime
from uuid import uuid4
import json

from homeassistant.const import (
    DEVICE_CLASS_POWER, POWER_WATT,
    DEVICE_CLASS_CURRENT, ELECTRICAL_CURRENT_AMPERE,
    DEVICE_CLASS_VOLTAGE, VOLT,
    DEVICE_CLASS_ENERGY, ENERGY_WATT_HOUR
)

from .const import *
from .switch import MerossLanSwitch
from .sensor import MerossLanSensor

def build_payload(namespace: str, method: str, payload: Any):
    messageid = uuid4().hex
    timestamp = int(time())
    p = {
            "header": {
                "messageId": messageid,
                "namespace": namespace,
                "method": method,
                "payloadVersion": 1,
                #"from": "/appliance/9109182170548290882048e1e9522946/publish",
                "timestamp": timestamp,
                "timestampMs": 0,
                "sign": md5((messageid + str(timestamp)).encode('utf-8')).hexdigest()
            },
            "payload": payload
        }
    return json.dumps(p)

class MerossDevice:

    def __init__(self, device_id: str, discoverypayload: Dict, async_mqtt_publish: Callable):
        self._device_id = device_id
        self._async_mqtt_publish = async_mqtt_publish  #provide the standard hass MQTT async_public signature
        self.ability = discoverypayload.get("ability", {})
        self.switches: [MerossLanSwitch] = []
        self.sensors: [MerossLanSensor] = []
        self._sensor_power = None
        self._sensor_current = None
        self._sensor_voltage = None
        self._sensor_energy = None
        self._online = False
        self.lastrequest = 0
        self.lastupdate = 0
        self.lastupdate_consumption = 0

        try:

            togglex = discoverypayload.get("all", {}).get("digest", {}).get("togglex")
            if isinstance(togglex, List):
                for t in togglex:
                    self.switches.append(MerossLanSwitch(self, t.get("channel"), self.togglex_set, self.togglex_get))
            elif isinstance(togglex, Dict):
                self.switches.append(MerossLanSwitch(self, togglex.get("channel"), self.togglex_set, self.togglex_get))
            elif NS_APPLIANCE_CONTROL_TOGGLEX in self.ability:
                #fallback for switches: in case we couldnt get from NS_APPLIANCE_SYSTEM_ALL
                self.switches.append(MerossLanSwitch(self, 0, self.togglex_set, self.togglex_get))
            elif NS_APPLIANCE_CONTROL_TOGGLE in self.ability:
                #fallback for switches: in case we couldnt get from NS_APPLIANCE_SYSTEM_ALL
                self.switches.append(MerossLanSwitch(self, 0, self.toggle_set, self.toggle_get))

            if NS_APPLIANCE_CONTROL_ELECTRICITY in self.ability:
                self._sensor_power = MerossLanSensor(self, DEVICE_CLASS_POWER, POWER_WATT)
                self.sensors.append(self._sensor_power)
                self._sensor_current = MerossLanSensor(self, DEVICE_CLASS_CURRENT, ELECTRICAL_CURRENT_AMPERE)
                self.sensors.append(self._sensor_current)
                self._sensor_voltage = MerossLanSensor(self, DEVICE_CLASS_VOLTAGE, VOLT)
                self.sensors.append(self._sensor_voltage)

            if NS_APPLIANCE_CONTROL_CONSUMPTIONX in self.ability:
                self._sensor_energy = MerossLanSensor(self, DEVICE_CLASS_ENERGY, ENERGY_WATT_HOUR)
                self.sensors.append(self._sensor_energy)

        except:
            pass

        return


    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def online(self) -> bool:
        #evaluate device MQTT availability by checking lastrequest got answered in less than 10 seconds
        if (self.lastupdate > self.lastrequest) or ((time() - self.lastrequest) < PARAM_UNAVAILABILITY_TIMEOUT):
            if not (self._online):
                self._online = True
                for switch in self.switches:
                    switch._set_available()
                for sensor in self.sensors:
                    sensor._set_available()
            return True
        #else
        if self._online:
            self._online = False
            for switch in self.switches:
                switch._set_unavailable()
            for sensor in self.sensors:
                sensor._set_unavailable()
        return False


    def triggerupdate(self) -> None:
        if not(self.online):
            self._mqtt_publish(NS_APPLIANCE_SYSTEM_ONLINE, METHOD_GET)
            return

        now = time()

        if NS_APPLIANCE_CONTROL_ELECTRICITY in self.ability:
            self._mqtt_publish(NS_APPLIANCE_CONTROL_ELECTRICITY, METHOD_GET)

        if self._sensor_energy and self._sensor_energy.enabled:
            if ((now - self.lastupdate_consumption) > PARAM_ENERGY_UPDATE_PERIOD):
                self._mqtt_publish(NS_APPLIANCE_CONTROL_CONSUMPTIONX, METHOD_GET)

        return

    def parsepayload(self, namespace: str, method: str, payload: Any) -> None:
        try:
            self.lastupdate = time()

            if namespace == NS_APPLIANCE_CONTROL_TOGGLEX:
                togglex = payload.get("togglex")
                if isinstance(togglex, List):
                    for t in togglex:
                        self.switches[t.get("channel")]._set_is_on(t.get("onoff") == 1)
                elif isinstance(togglex, Dict):
                    self.switches[togglex.get("channel")]._set_is_on(togglex.get("onoff") == 1)

            elif namespace == NS_APPLIANCE_CONTROL_ELECTRICITY:
                electricity = payload.get("electricity")
                power_w = electricity.get("power") / 1000
                voltage_v = electricity.get("voltage") / 10
                current_a = electricity.get("current") / 1000
                if self._sensor_power:
                    self._sensor_power._set_state(power_w)
                if self._sensor_current:
                    self._sensor_current._set_state(current_a)
                if self._sensor_voltage:
                    self._sensor_voltage._set_state(voltage_v)
                """ TAG_NOPOWERATTR
                disable attributes publishing to avoid unnecessary recording on switch entity
                power readings are now available as proper sensor entities

                self.switches[electricity.get("channel")]._set_power(power_w, voltage_v, current_a)
                """

            elif namespace == NS_APPLIANCE_CONTROL_CONSUMPTIONX:
                if self._sensor_energy:
                    self.lastupdate_consumption = self.lastupdate
                    daylabel = strftime("%Y-%m-%d", localtime())
                    for d in payload.get("consumptionx"):
                        if d.get("date") == daylabel:
                            energy_wh = d.get("value")
                            self._sensor_energy._set_state(energy_wh)
                            """ TAG_NOPOWERATTR
                            disable attributes publishing to avoid unnecessary recording on switch entity
                            power readings are now available as proper sensor entities

                            self.switches[0]._set_energy(energy_wh / 1000)
                            """

        except:
            pass

        return


    def toggle_set(self, channel: int, ison: int):
        return self._mqtt_publish(
            NS_APPLIANCE_CONTROL_TOGGLE,
            METHOD_SET,
            {"toggle": {"channel": channel, "onoff": ison}}
        )

    def toggle_get(self, channel: int):
        return self._mqtt_publish(
            NS_APPLIANCE_CONTROL_TOGGLE,
            METHOD_GET,
            {"toggle": {"channel": channel}}
        )

    def togglex_set(self, channel: int, ison: int):
        return self._mqtt_publish(
            NS_APPLIANCE_CONTROL_TOGGLEX,
            METHOD_SET,
            {"togglex": {"channel": channel, "onoff": ison}}
        )

    def togglex_get(self, channel: int):
        return self._mqtt_publish(
            NS_APPLIANCE_CONTROL_TOGGLEX,
            METHOD_GET,
            {"togglex": {"channel": channel}}
        )


    def _mqtt_publish(self, namespace: str, method: str, payload: Dict = {}):
        # self.lastrequest should represent the time of the most recent un-responded request
        if self.lastupdate >= self.lastrequest:
            self.lastrequest = time()
        mqttpayload = build_payload(namespace, method, payload)
        return self._async_mqtt_publish(COMMAND_TOPIC.format(self._device_id), mqttpayload, 0, False)
