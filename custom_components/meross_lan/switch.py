import logging
from typing import Any, Callable, Dict, List, Optional

#import json

#from hashlib import md5
#from time import time
#from uuid import uuid4

#from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.typing import HomeAssistantType
#import homeassistant.helpers.device_registry as device_registry

from .const import *


async def async_setup_entry(hass: HomeAssistantType, config_entry: ConfigEntry, async_add_devices):
    device_id = config_entry.data[CONF_DEVICE_ID]
    async_add_devices(hass.data[DOMAIN].devices[device_id].switches)
    return


