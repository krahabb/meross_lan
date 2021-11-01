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
    """
    check if any MQTT is configured
    """
    return hass.data.get(DATA_MQTT) is not None

def mqtt_is_connected(hass) -> bool:
    """
    check if MQTT communication is available
    """
    mqtt = hass.data.get(DATA_MQTT)
    return mqtt.connected if mqtt is not None else False

def mqtt_publish(hass, topic, payload):
    """
    friendly 'publish' to bypass official core/mqtt interface variations
    this could be dangerous on compatibility but the ongoing api changes (2021.12.0)
    are a bit too much to follow with a clean backward compatible code
    """
    hass.async_create_task(hass.data[DATA_MQTT].async_publish(topic, payload, 0, False))
