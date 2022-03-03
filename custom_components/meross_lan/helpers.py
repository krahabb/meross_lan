"""
    Helpers!
"""
import logging
from time import time

from .merossclient import const as mc

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


def clamp(_value, _min, _max):
    if _value >= _max:
        return _max
    elif _value <= _min:
        return _min
    else:
        return _value


def reverse_lookup(map: dict, value):
    """
    lookup the values in map (dict) and return
    the corresponding key
    """
    for _key, _value in map.items():
        if _value == value:
            return _key
    return None


"""
    obfuscation:
    call obfuscate on a paylod (dict) to remove well-known sensitive
    keys (list in OBFUSCATE_KEYS). The returned dictionary contains a
    copy of original values and need to be used a gain when calling
    deobfuscate on the previously obfuscated payload
"""
OBFUSCATE_KEYS = (
    mc.KEY_UUID, mc.KEY_MACADDRESS, mc.KEY_WIFIMAC, mc.KEY_INNERIP,
    mc.KEY_SERVER, mc.KEY_PORT, mc.KEY_SECONDSERVER, mc.KEY_SECONDPORT,
    mc.KEY_USERID, mc.KEY_TOKEN,
)


def obfuscate(payload: dict) -> dict:
    """
    payload: input-output gets modified by blanking sensistive keys
    returns: a dict with the original mapped obfuscated keys
    parses the input payload and 'hides' (obfuscates) some sensitive keys.
    returns the mapping of the obfuscated keys in 'obfuscated' so to re-set them in _deobfuscate
    this function is recursive
    """
    obfuscated = dict()
    for key, value in payload.items():
        if isinstance(value, dict):
            o = obfuscate(value)
            if o:
                obfuscated[key] = o
        elif key in OBFUSCATE_KEYS:
            obfuscated[key] = value
            payload[key] = '#' * len(str(value))

    return obfuscated


def deobfuscate(payload: dict, obfuscated: dict):
    for key, value in obfuscated.items():
        if isinstance(value, dict):
            deobfuscate(payload[key], value)
        else:
            payload[key] = value


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
