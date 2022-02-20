from __future__ import annotations

from homeassistant.components.number import (
    DOMAIN as PLATFORM_NUMBER,
    NumberEntity,
)
try:
    from homeassistant.components.number import NumberMode
    NUMBERMODE_AUTO = NumberMode.AUTO
    NUMBERMODE_BOX = NumberMode.BOX
    NUMBERMODE_SLIDER = NumberMode.SLIDER
except:
    NUMBERMODE_AUTO = "auto"
    NUMBERMODE_BOX = "box"
    NUMBERMODE_SLIDER = "slider"

from .merossclient import const as mc, get_namespacekey  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
    ENTITY_CATEGORY_CONFIG,
)



async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_NUMBER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_NUMBER)


class MLConfigNumber(_MerossEntity, NumberEntity):

    PLATFORM = PLATFORM_NUMBER

    multiplier = 1

    _attr_mode = NUMBERMODE_BOX

    @property
    def entity_category(self):
        return ENTITY_CATEGORY_CONFIG


    @property
    def value(self) -> float | None:
        return self._attr_state


    def update_value(self, value):
        self.update_state(value / self.multiplier)


class MLHubAdjustNumber(MLConfigNumber):

    multiplier = 100

    def __init__(
        self,
        subdevice: "MerossSubDevice",
        key: str,
        namespace: str,
        label: str,
        device_class: str,
        min_value: float,
        max_value: float,
        step: float
        ):
        self._key = key
        self._namespace = namespace
        self._namespace_key = get_namespacekey(namespace)
        self._label = label
        self._attr_min_value = min_value
        self._attr_max_value = max_value
        self._attr_step = step
        super().__init__(
            subdevice.hub,
            subdevice.id,
            f"config_{self._namespace_key}_{key}",
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
                        self._key: int(value * self.multiplier)
                    }
                ]
            },
        )


