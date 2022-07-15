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

CORE_HAS_NATIVE_UNIT = hasattr(NumberEntity, 'native_unit_of_measurement')

from .merossclient import const as mc, get_namespacekey  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
    ENTITY_CATEGORY_CONFIG,
)
from .sensor import CLASS_TO_UNIT_MAP


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_NUMBER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_NUMBER)


if CORE_HAS_NATIVE_UNIT:
    # implement 'new' (2022.6) style NumberEntity
    class MLConfigNumber(_MerossEntity, NumberEntity):

        PLATFORM = PLATFORM_NUMBER

        multiplier = 1

        _attr_mode = NUMBERMODE_BOX
        _attr_native_max_value: float
        _attr_native_min_value: float
        _attr_native_step: float
        _attr_native_unit_of_measurement: str | None


        @property
        def entity_category(self):
            return ENTITY_CATEGORY_CONFIG

        @property
        def native_max_value(self):
            return self._attr_native_max_value

        @property
        def native_min_value(self):
            return self._attr_native_min_value

        @property
        def native_step(self):
            return self._attr_native_step

        @property
        def native_unit_of_measurement(self):
            return self._attr_native_unit_of_measurement

        @property
        def native_value(self) -> float | None:
            return self._attr_state

        def update_native_value(self, value):
            self.update_state(value / self.multiplier)

else:
    # pre 2022.6 style NumberEntity
    # since derived classes will try to adapt to new _native_* style
    # here we adapt for older HA cores
    class MLConfigNumber(_MerossEntity, NumberEntity):

        PLATFORM = PLATFORM_NUMBER

        multiplier = 1

        _attr_mode = NUMBERMODE_BOX
        _attr_native_max_value: float
        _attr_native_min_value: float
        _attr_native_step: float
        _attr_native_unit_of_measurement: str | None


        @property
        def entity_category(self):
            return ENTITY_CATEGORY_CONFIG

        @property
        def native_max_value(self):
            return self._attr_native_max_value

        @property
        def native_min_value(self):
            return self._attr_native_min_value

        @property
        def native_step(self):
            return self._attr_native_step

        @property
        def native_unit_of_measurement(self):
            return self._attr_native_unit_of_measurement

        @property
        def max_value(self) -> float:
            return self.native_max_value

        @property
        def min_value(self) -> float:
            return self.native_min_value

        @property
        def step(self) -> float:
            return self.native_step

        @property
        def unit_of_measurement(self) -> str | None:
            return self.native_unit_of_measurement

        @property
        def value(self) -> float | None:
            return self._attr_state

        def update_native_value(self, value):
            self.update_state(value / self.multiplier)

        async def async_set_value(self, value: float):
            await self.async_set_native_value(value)


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
        self._attr_native_min_value = min_value
        self._attr_native_max_value = max_value
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = CLASS_TO_UNIT_MAP.get(device_class)
        super().__init__(
            subdevice.hub,
            subdevice.id,
            f"config_{self._namespace_key}_{key}",
            device_class,
            subdevice)


    @property
    def name(self) -> str:
        return f"{self.subdevice.name} - adjust {self._label} {self._attr_device_class}"


    async def async_set_native_value(self, value: float):
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


