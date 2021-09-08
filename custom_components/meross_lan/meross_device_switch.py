import logging
from time import localtime
from datetime import datetime, timedelta, timezone

from homeassistant.const import (
    DEVICE_CLASS_POWER,
    DEVICE_CLASS_CURRENT,
    DEVICE_CLASS_VOLTAGE,
    DEVICE_CLASS_ENERGY,
)

from .merossclient import KeyType, const as mc  # mEROSS cONST
from .meross_entity import MerossFakeEntity
from .sensor import MerossLanSensor, STATE_CLASS_TOTAL_INCREASING
from .switch import MerossLanSwitch
from .meross_device import MerossDevice
from .helpers import LOGGER
from .const import PARAM_ENERGY_UPDATE_PERIOD

class MerossDeviceSwitch(MerossDevice):

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)
        self._sensor_power = MerossFakeEntity
        self._sensor_current = MerossFakeEntity
        self._sensor_voltage = MerossFakeEntity
        self._sensor_energy = MerossFakeEntity
        self._energy_lastupdate = 0
        self._energy_last_reset = 0 # store the last 'device time' we passed onto to _attr_last_reset

        try:
            # use a mix of heuristic to detect device features
            ability = self.descriptor.ability
            # atm we're not sure we can detect this in 'digest' payload
            # looks like mrs100 just exposes abilities and we'll have to poll
            # related NS
            if mc.NS_APPLIANCE_ROLLERSHUTTER_STATE in ability:
                from .cover import MerossLanRollerShutter
                MerossLanRollerShutter(self, 0)
                self.polling_dictionary[mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION] = { mc.KEY_POSITION : [] }
                self.polling_dictionary[mc.NS_APPLIANCE_ROLLERSHUTTER_STATE] = { mc.KEY_STATE : [] }
            else:
                p_digest = self.descriptor.digest
                if p_digest:
                    garagedoor = p_digest.get(mc.KEY_GARAGEDOOR)
                    if isinstance(garagedoor, list):
                        from .cover import MerossLanGarage
                        for g in garagedoor:
                            MerossLanGarage(self, g.get(mc.KEY_CHANNEL))
                    spray = p_digest.get(mc.KEY_SPRAY)
                    if isinstance(spray, list):
                        try:
                            from .select import MerossLanSpray
                        except:# SELECT entity platform added later in 2021
                            LOGGER.warning("MerossDeviceSwitch(%s):"
                                " missing 'select' entity type. Please update HA to latest version"
                                " to fully support this device. Falling back to basic switch behaviour"
                                , self.device_id)
                            from .switch import MerossLanSpray
                        for s in spray:
                            MerossLanSpray(self, s.get(mc.KEY_CHANNEL))
                    # at any rate: setup switches whenever we find 'togglex'
                    # or whenever we cannot setup anything from digest
                    togglex = p_digest.get(mc.KEY_TOGGLEX)
                    if isinstance(togglex, list):
                        for t in togglex:
                            channel = t.get(mc.KEY_CHANNEL)
                            if channel not in self.entities:
                                MerossLanSwitch(
                                    self,
                                    channel,
                                    mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                                    mc.KEY_TOGGLEX)
                    elif isinstance(togglex, dict):
                        channel = togglex.get(mc.KEY_CHANNEL)
                        if channel not in self.entities:
                            MerossLanSwitch(
                                self,
                                channel,
                                mc.NS_APPLIANCE_CONTROL_TOGGLEX,
                                mc.KEY_TOGGLEX)
                    #endif p_digest
                else:
                    # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
                    # but have 'control'
                    p_control = self.descriptor.all.get(mc.KEY_CONTROL)
                    if p_control:
                        p_toggle = p_control.get(mc.KEY_TOGGLE)
                        if isinstance(p_toggle, dict):
                            MerossLanSwitch(
                                self,
                                p_toggle.get(mc.KEY_CHANNEL, 0),
                                mc.NS_APPLIANCE_CONTROL_TOGGLE,
                                mc.KEY_TOGGLE)

            #fallback for switches: in case we couldnt get from NS_APPLIANCE_SYSTEM_ALL
            if not self.entities:
                if mc.NS_APPLIANCE_CONTROL_TOGGLEX in ability:
                    MerossLanSwitch(self, 0, mc.NS_APPLIANCE_CONTROL_TOGGLEX, mc.KEY_TOGGLEX)
                elif mc.NS_APPLIANCE_CONTROL_TOGGLE in ability:
                    MerossLanSwitch(self, 0, mc.NS_APPLIANCE_CONTROL_TOGGLE, mc.KEY_TOGGLE)

            if mc.NS_APPLIANCE_CONTROL_ELECTRICITY in ability:
                self._sensor_power = MerossLanSensor(self, DEVICE_CLASS_POWER, DEVICE_CLASS_POWER)
                self._sensor_current = MerossLanSensor(self, DEVICE_CLASS_CURRENT, DEVICE_CLASS_CURRENT)
                self._sensor_voltage = MerossLanSensor(self, DEVICE_CLASS_VOLTAGE, DEVICE_CLASS_VOLTAGE)

            if mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX in ability:
                self._sensor_energy = MerossLanSensor(self, DEVICE_CLASS_ENERGY, DEVICE_CLASS_ENERGY)
                self._sensor_energy._attr_state_class = STATE_CLASS_TOTAL_INCREASING

        except Exception as e:
            LOGGER.warning("MerossDeviceSwitch(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_TOGGLE:
            self._parse_togglex(payload.get(mc.KEY_TOGGLE))
            return True

        if namespace == mc.NS_APPLIANCE_GARAGEDOOR_STATE:
            self._parse_garageDoor(payload.get(mc.KEY_STATE))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_STATE:
            self._parse_rollershutter_state(payload.get(mc.KEY_STATE))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION:
            if method == mc.METHOD_SETACK:
                """
                the SETACK PAYLOAD is empty so no info to extract but we'll use it
                as a trigger to request status update so to refresh movement state
                """
                self.request(mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION,
                    mc.METHOD_GET, { mc.KEY_POSITION : [] })
                self.request(mc.NS_APPLIANCE_ROLLERSHUTTER_STATE,
                    mc.METHOD_GET, { mc.KEY_STATE : [] })
            else:
                self._parse_rollershutter_position(payload.get(mc.KEY_POSITION))
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_ELECTRICITY:
            electricity = payload.get(mc.KEY_ELECTRICITY)
            self._sensor_power._set_state(electricity.get(mc.KEY_POWER) / 1000)
            self._sensor_current._set_state(electricity.get(mc.KEY_CURRENT) / 1000)
            self._sensor_voltage._set_state(electricity.get(mc.KEY_VOLTAGE) / 10)
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX:
            self._energy_lastupdate = self.lastupdate
            days = payload.get(mc.KEY_CONSUMPTIONX)
            days_len = len(days)
            if days_len < 1:
                self._sensor_energy._attr_last_reset = datetime.utcfromtimestamp(0)
                self._sensor_energy._set_state(0)
                return True
            # we'll look through the device array values to see
            # data timestamped (in device time) after last midnight
            # since we usually reset this around midnight localtime
            # the device timezone should be aligned else it will roundtrip
            # against it's own midnight and we'll see a delayed 'sawtooth'
            st = localtime()
            dt = datetime(
                st.tm_year, st.tm_mon, st.tm_mday,
                tzinfo=timezone(timedelta(seconds=st.tm_gmtoff), st.tm_zone)
            )
            timestamp_last_reset = dt.timestamp() - self.device_timedelta
            self.log(
                logging.DEBUG, 0,
                "MerossDevice(%s) Energy: device midnight = %d",
                self.device_id, timestamp_last_reset
            )
            def get_timestamp(day):
                return day.get(mc.KEY_TIME)
            days = sorted(days, key=get_timestamp, reverse=True)
            day_last:dict = days[0]
            if day_last.get(mc.KEY_TIME) < timestamp_last_reset:
                return True
            if days_len > 1:
                timestamp_last_reset = days[1].get(mc.KEY_TIME)
            if self._energy_last_reset != timestamp_last_reset:
                # we 'cache' timestamp_last_reset so we don't 'jitter' _attr_last_reset
                # should device_timedelta change (and it will!)
                # this is not really working until days_len is >= 2
                self._energy_last_reset = timestamp_last_reset
                # we'll add .5 (sec) to the device last reading since the reset
                # occurs right after that
                self._sensor_energy._attr_last_reset = datetime.utcfromtimestamp(
                    timestamp_last_reset + self.device_timedelta + .5
                )
                self.log(
                    logging.DEBUG, 0,
                    "MerossDevice(%s) Energy: update last_reset to %s",
                    self.device_id, self._sensor_energy._attr_last_reset.isoformat()
                )
            self._sensor_energy._set_state(day_last.get(mc.KEY_VALUE))
            return True

        if namespace == mc.NS_APPLIANCE_CONTROL_SPRAY:
            self._parse_spray(payload.get(mc.KEY_SPRAY))
            return True

        return False


    def _parse_garageDoor(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL, 0)]._set_open(payload.get(mc.KEY_OPEN), payload.get(mc.KEY_EXECUTE))
        elif isinstance(payload, list):
            for p in payload:
                self._parse_garageDoor(p)


    def _parse_rollershutter_state(self, p_state) -> None:
        if isinstance(p_state, dict):
            self.entities[p_state.get(mc.KEY_CHANNEL, 0)]._set_rollerstate(p_state.get(mc.KEY_STATE))
        elif isinstance(p_state, list):
            for s in p_state:
                self._parse_rollershutter_state(s)


    def _parse_rollershutter_position(self, p_position) -> None:
        if isinstance(p_position, dict):
            self.entities[p_position.get(mc.KEY_CHANNEL, 0)]._set_rollerposition(p_position.get(mc.KEY_POSITION))
        elif isinstance(p_position, list):
            for p in p_position:
                self._parse_rollershutter_position(p)


    def _parse_spray(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL, 0)]._set_mode(payload.get(mc.KEY_MODE))
        elif isinstance(payload, list):
            for p in payload:
                self._parse_spray(p)


    def _parse_all(self, payload: dict) -> None:
        super()._parse_all(payload)

        p_digest = self.descriptor.digest
        if p_digest:
            pass
        else:
            # older firmwares (MSS110 with 1.1.28) look like dont really have 'digest'
            p_control = self.descriptor.all.get(mc.KEY_CONTROL)
            if isinstance(p_control, dict):
                self._parse_togglex(p_control.get(mc.KEY_TOGGLE))


    def _request_updates(self, epoch, namespace):
        super()._request_updates(epoch, namespace)
        # we're not checking context namespace since it should be very unusual
        # to enter here with one of those following
        if self._sensor_power.enabled or self._sensor_voltage.enabled or self._sensor_current.enabled:
            self.request(mc.NS_APPLIANCE_CONTROL_ELECTRICITY)
        if self._sensor_energy.enabled:
            if ((epoch - self._energy_lastupdate) > PARAM_ENERGY_UPDATE_PERIOD):
                self.request(mc.NS_APPLIANCE_CONTROL_CONSUMPTIONX)