from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity

from custom_components.meross_lan.meross_device import MerossDevice
from custom_components.meross_lan.meross_device_hub import MerossSubDevice
from custom_components.meross_lan.meross_entity import MerossEntity

from tests.helpers import DeviceContext

EntityType = type[Entity]
MerossEntityTypeSet = set[type[MerossEntity]]


class EntityTestContext:
    hass: HomeAssistant
    ability: dict[str, Any]
    expected_entity_types: MerossEntityTypeSet

    device_context: DeviceContext
    device: MerossDevice
    subdevice: MerossSubDevice | None
    entity_id: str | None


class EntityComponentTest:

    ENTITY_TYPE: EntityType
    DIGEST_ENTITIES: dict[str, MerossEntityTypeSet] = {}
    NAMESPACES_ENTITIES: dict[str, MerossEntityTypeSet] = {}
    HUB_SUBDEVICES_ENTITIES: dict[str, MerossEntityTypeSet] = {}

    async def async_test_each_callback(
        self, context: EntityTestContext, entity: MerossEntity
    ):
        pass

    async def async_test_enabled_callback(
        self, context: EntityTestContext, entity: MerossEntity, entity_id: str
    ):
        pass

    async def async_test_disabled_callback(
        self, context: EntityTestContext, entity: MerossEntity
    ):
        pass
