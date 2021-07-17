"""
    Helpers!
"""
import logging
from time import time

LOGGER = logging.getLogger(__name__[:-8]) #get base custom_component name for logging
_trap_msg = None
_trap_args = None
_trap_time = 0
_trap_level = 0

def LOGGER_trap(level, timeout, msg, *args):
    """
    avoid repeating the same last log message until something changes or timeout expires
    used mainly when discovering new devices
    """
    global _trap_msg
    global _trap_args
    global _trap_time
    global _trap_level

    epoch = time()
    if (_trap_level == level) \
        and (_trap_msg == msg) \
        and (_trap_args == args) \
        and ((epoch - _trap_time) < timeout):
        return

    LOGGER.log(level, msg, *args)
    _trap_msg = msg
    _trap_args = args
    _trap_time = epoch
    _trap_level = level


"""
MQTT helpers
"""
from homeassistant.components.mqtt import DATA_MQTT

def mqtt_is_loaded(hass) -> bool:
    return hass.data.get(DATA_MQTT) is not None

def mqtt_is_connected(hass) -> bool:
    _mqtt = hass.data.get(DATA_MQTT)
    return _mqtt.connected if _mqtt is not None else False
