"""
    Helpers!
"""
import logging
from time import time

LOGGER = logging.getLogger(__name__[:-8]) #get base custom_component name for logging
_trap_dict = dict()

def LOGGER_trap(level, timeout, msg, *args):
    """
    avoid repeating the same last log message until something changes or timeout expires
    used mainly when discovering new devices
    """
    global _trap_dict

    epoch = time()
    trap_key = (msg, *args)
    trap_time = _trap_dict.get(trap_key, 0)
    if ((epoch - trap_time) < timeout):
        return

    LOGGER.log(level, msg, *args)
    _trap_dict[trap_key] = epoch


"""
MQTT helpers
"""
from homeassistant.components.mqtt import DATA_MQTT

def mqtt_is_loaded(hass) -> bool:
    return hass.data.get(DATA_MQTT) is not None

def mqtt_is_connected(hass) -> bool:
    _mqtt = hass.data.get(DATA_MQTT)
    return _mqtt.connected if _mqtt is not None else False
