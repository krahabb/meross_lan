from __future__ import annotations

from homeassistant.const import (
    DEVICE_CLASS_TEMPERATURE,
)
from homeassistant.components.number import (
    DOMAIN as PLATFORM_NUMBER,
    NumberEntity,
)
from homeassistant.components.number.const import (
    DEFAULT_MIN_VALUE, DEFAULT_MAX_VALUE, DEFAULT_STEP,
)

from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
    ENTITY_CATEGORY_CONFIG,
)
from .climate import (
    MtsClimate,
    PRESET_COMFORT, PRESET_SLEEP, PRESET_AWAY,
)
from .merossclient import const as mc, get_namespacekey  # mEROSS cONST


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_NUMBER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_NUMBER)


class MLNumber(_MerossEntity, NumberEntity):

    PLATFORM = PLATFORM_NUMBER


    def __init__(
        self,
        device,
        channel: object,
        entitykey: str,
        min_value: float = DEFAULT_MIN_VALUE,
        max_value: float = DEFAULT_MAX_VALUE,
        step: float = DEFAULT_STEP,
        device_class: str = None,
        subdevice = None
        ):
        super().__init__(device, channel, entitykey, device_class, subdevice)
        self._attr_min_value = min_value
        self._attr_max_value = max_value
        self._attr_step = step


    @property
    def entity_category(self):
        return ENTITY_CATEGORY_CONFIG

    @property
    def min_value(self) -> float:
        return self._attr_min_value

    @property
    def max_value(self) -> float:
        return self._attr_max_value

    @property
    def step(self) -> float:
        return self._attr_step

    @property
    def value(self) -> float | None:
        return self._attr_state



class MLHubAdjustNumber(MLNumber):


    def __init__(
        self,
        subdevice: "MerossSubDevice",
        key: str,
        namespace: str,
        label: str,
        device_class: str,
        multiplier: float,
        min_value: float,
        max_value: float,
        step: float
        ):
        self._key = key
        self._namespace = namespace
        self._namespace_key = get_namespacekey(namespace)
        self._label = label
        self._multiplier = multiplier
        super().__init__(
            subdevice.hub,
            subdevice.id,
            f"config_{self._namespace_key}_{key}",
            min_value,
            max_value,
            step,
            device_class,
            subdevice)


    @property
    def name(self) -> str:
        return f"{self.subdevice.name} - adjust {self._label} {self._attr_device_class}"


    async def async_set_value(self, value: float) -> None:

        self.device.request(
            self._namespace,
            mc.METHOD_SET,
            {
                self._namespace_key: [
                    {
                        mc.KEY_ID: self.subdevice.id,
                        self._key: int(value * self._multiplier)
                    }
                ]
            },
        )


    def update_value(self, value):
        self.update_state(value / self._multiplier)



class MtsSetPointNumber(_MerossEntity, NumberEntity):
    """
    Helper entity to configure MTS (thermostats) setpoints
    AKA: Heat(comfort) - Cool(sleep) - Eco(away)
    """
    PLATFORM = PLATFORM_NUMBER

    PRESET_TO_ICON_MAP = {
        PRESET_COMFORT: 'mdi:sun-thermometer',
        PRESET_SLEEP: 'mdi:power-sleep',
        PRESET_AWAY: 'mdi:bag-checked',
    }

    def __init__(self, climate: MtsClimate, preset_mode: str):
        self._climate = climate
        self._preset_mode = preset_mode
        self._key = climate.PRESET_TO_TEMPERATUREKEY_MAP[preset_mode]
        self._attr_icon = self.PRESET_TO_ICON_MAP[preset_mode]
        super().__init__(
            climate.device,
            climate.channel,
            f"config_{mc.KEY_TEMPERATURE}_{self._key}",
            DEVICE_CLASS_TEMPERATURE,
            climate.subdevice
        )

    @property
    def entity_category(self):
        return ENTITY_CATEGORY_CONFIG

    @property
    def name(self) -> str:
        return f"{self._climate.name} - {self._preset_mode} {DEVICE_CLASS_TEMPERATURE}"

    @property
    def step(self) -> float:
        return self._climate._attr_target_temperature_step

    @property
    def min_value(self) -> float:
        return self._climate._attr_min_temp

    @property
    def max_value(self) -> float:
        return self._climate._attr_max_temp

    @property
    def value(self) -> float | None:
        return self._attr_state