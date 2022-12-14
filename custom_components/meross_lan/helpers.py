"""
    Helpers!
"""
import logging
from functools import partial
from time import time
from homeassistant.util.dt import utcnow

from .merossclient import const as mc

LOGGER = logging.getLogger(__name__[:-8]) #get base custom_component name for logging
_TRAP_DICT = dict()

def LOGGER_trap(level, timeout, msg, *args):
    """
    avoid repeating the same last log message until something changes or timeout expires
    used mainly when discovering new devices
    """
    global _TRAP_DICT

    epoch = time()
    trap_key = (msg, *args)
    trap_time = _TRAP_DICT.get(trap_key, 0)
    if ((epoch - trap_time) < timeout):
        return

    LOGGER.log(level, msg, *args)
    _TRAP_DICT[trap_key] = epoch


def clamp(_value, _min, _max):
    """
    saturate _value between _min and _max
    """
    if _value >= _max:
        return _max
    elif _value <= _min:
        return _min
    else:
        return _value


def reverse_lookup(_dict: dict, value):
    """
    lookup the values in map (dict) and return
    the corresponding key
    """
    for _key, _value in _dict.items():
        if _value == value:
            return _key
    return None


def versiontuple(version: str) -> tuple:
    """
    helper for version checking, comparisons, etc
    """
    return tuple(map(int, (version.split("."))))


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
def mqtt_is_loaded(hass) -> bool:
    """
    check if any MQTT is configured
    """
    try:
        # implemented since 2022.9.x or so...
        from homeassistant.components.mqtt.util import get_mqtt_data
        if (mqtt_data := get_mqtt_data(hass, False)):
            return mqtt_data.client is not None
        return False
    except:
        # legacy config/client check
        from homeassistant.components.mqtt import DATA_MQTT
        return hass.data.get(DATA_MQTT) is not None


def mqtt_is_connected(hass) -> bool:
    """
    check if MQTT communication is available
    """
    from homeassistant.components.mqtt import is_connected
    return is_connected(hass)


def mqtt_publish(hass, topic, payload):
    """
    friendly 'publish' to bypass official core/mqtt interface variations
    this could be dangerous on compatibility but the ongoing api changes (2021.12.0)
    are a bit too much to follow with a clean backward compatible code
    EDIT 2022-09-29:
    following recent issues (#213 - HA core 2022.9.6) this code is reverted
    to using the official api for the mqtt component. In doing so we're likely
    breaking compatibility with pre 2021.12.0
    """
    from homeassistant.components.mqtt import publish
    publish(hass, topic, payload)


"""
RECORDER helpers
"""
from homeassistant.components.recorder import history

async def get_entity_last_state(hass, entity_id):
    """
    recover the last known good state from recorder in order to
    restore transient state information when restarting HA
    """
    if hasattr(history, 'get_state'):# removed in 2022.6.x
        return history.get_state(hass, utcnow(), entity_id)

    elif hasattr(history, 'get_last_state_changes'):
        """
        get_instance too is relatively new: I hope it was in place when
        get_last_state_changes was added
        """
        from homeassistant.components.recorder import get_instance
        _last_state: dict = await get_instance(hass).async_add_executor_job(
                partial(
                    history.get_last_state_changes,
                    hass,
                    1,
                    entity_id,
                )
            )
        if entity_id in _last_state:
            _last_state: list = _last_state[entity_id]
            if _last_state:
                return _last_state[0]


    return None
