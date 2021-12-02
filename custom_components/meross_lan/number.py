from __future__ import annotations

from homeassistant.components.number import (
    DOMAIN as PLATFORM_NUMBER,
    NumberEntity,
)
from homeassistant.components.number.const import (
    DEFAULT_MIN_VALUE,
    DEFAULT_MAX_VALUE,
    DEFAULT_STEP,
)

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
    ENTITY_CATEGORY_CONFIG,
)


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_NUMBER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_NUMBER)


class MerossLanNumber(_MerossEntity, NumberEntity):

    PLATFORM = PLATFORM_NUMBER


    def __init__(
        self,
        device: "MerossDevice",
        _id: object,
        min_value: float = DEFAULT_MIN_VALUE,
        max_value: float = DEFAULT_MAX_VALUE,
        step: float = DEFAULT_STEP,
        device_class: str = None,
        subdevice: "MerossSubDevice" = None
        ):
        super().__init__(device, _id, device_class, subdevice)
        self._attr_min_value = min_value
        self._attr_max_value = max_value
        self._attr_step = step


    @property
    def entity_category(self) -> str | None:
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



class MerossLanHubAdjustNumber(MerossLanNumber):


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
        self._namespace_key = namespace.split('.')[-1].lower()
        self._label = label
        self._multiplier = multiplier
        super().__init__(
            subdevice.hub,
            f"{subdevice.id}_config_{self._namespace_key}_{key}",
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
