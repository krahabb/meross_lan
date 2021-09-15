from .merossclient import const as mc  # mEROSS cONST
from .meross_device import MerossDevice
from .helpers import LOGGER


class MerossDeviceGarage(MerossDevice):

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
            p_digest = self.descriptor.digest
            if p_digest:
                garagedoor = p_digest.get(mc.KEY_GARAGEDOOR)
                if isinstance(garagedoor, list):
                    from .cover import MerossLanGarage
                    for g in garagedoor:
                        MerossLanGarage(self, g.get(mc.KEY_CHANNEL))
                #endif p_digest

        except Exception as e:
            LOGGER.warning("MerossDeviceGarage(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_GARAGEDOOR_STATE:
            self._parse_garageDoor(payload.get(mc.KEY_STATE))
            return True

        return False


    def _parse_garageDoor(self, payload) -> None:
        if isinstance(payload, dict):
            self.entities[payload.get(mc.KEY_CHANNEL, 0)]._set_open(
                payload.get(mc.KEY_OPEN),
                payload.get(mc.KEY_EXECUTE)
            )
        elif isinstance(payload, list):
            for p in payload:
                self._parse_garageDoor(p)



class MerossDeviceShutter(MerossDevice):

    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)

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
                self.polling_dictionary[mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG] = { mc.KEY_CONFIG : [] }

        except Exception as e:
            LOGGER.warning("MerossDeviceShutter(%s) init exception:(%s)", self.device_id, str(e))


    def receive(
        self,
        namespace: str,
        method: str,
        payload: dict,
        header: dict
    ) -> bool:

        if super().receive(namespace, method, payload, header):
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_STATE:
            self._parse_rollershutter_state(payload.get(mc.KEY_STATE))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_POSITION:
            if method == mc.METHOD_SETACK:
                """
                the SETACK PAYLOAD is empty so no info to extract but we'll use it
                as a trigger to request status update so to refresh movement state
                code moved to _ack_callback in MerossLanRollerShutter
                """
            else:
                self._parse_rollershutter_position(payload.get(mc.KEY_POSITION))
            return True

        if namespace == mc.NS_APPLIANCE_ROLLERSHUTTER_CONFIG:
            # payload = {"config": [{"channel": 0, "signalOpen": 50000, "signalClose": 50000}]}
            self._parse_rollershutter_config(payload.get(mc.KEY_CONFIG))
        return False


    def _parse_rollershutter_state(self, p_state) -> None:
        if isinstance(p_state, dict):
            self.entities[p_state.get(mc.KEY_CHANNEL, 0)]._set_rollerstate(
                p_state.get(mc.KEY_STATE)
            )
        elif isinstance(p_state, list):
            for s in p_state:
                self._parse_rollershutter_state(s)


    def _parse_rollershutter_position(self, p_position) -> None:
        if isinstance(p_position, dict):
            self.entities[p_position.get(mc.KEY_CHANNEL, 0)]._set_rollerposition(
                p_position.get(mc.KEY_POSITION)
            )
        elif isinstance(p_position, list):
            for p in p_position:
                self._parse_rollershutter_position(p)


    def _parse_rollershutter_config(self, p_config) -> None:
        if isinstance(p_config, dict):
            self.entities[p_config.get(mc.KEY_CHANNEL, 0)]._set_rollerconfig(
                p_config.get(mc.KEY_SIGNALOPEN),
                p_config.get(mc.KEY_SIGNALCLOSE)
            )
        elif isinstance(p_config, list):
            for p in p_config:
                self._parse_rollershutter_config(p)