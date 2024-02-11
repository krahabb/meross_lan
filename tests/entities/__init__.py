from typing import Any, ClassVar

from homeassistant.core import HomeAssistant, StateMachine
from homeassistant.helpers.entity import Entity

from custom_components.meross_lan.meross_device import MerossDevice
from custom_components.meross_lan.meross_device_hub import MerossSubDevice
from custom_components.meross_lan.meross_entity import MerossEntity

from tests.helpers import DeviceContext

EntityType = type[Entity]
MerossEntityTypeSet = set[type[MerossEntity]]


class EntityComponentTest:
    """
    Provides an interface for each entity domain to execute
    proper testing on the different test types.
    """

    # static test context
    hass: ClassVar[HomeAssistant]
    hass_service_call: ClassVar
    hass_states: ClassVar[StateMachine]
    ability: ClassVar[dict[str, Any]]
    digest: ClassVar[dict[str, Any]]
    expected_entity_types: ClassVar[MerossEntityTypeSet]
    device_context: ClassVar[DeviceContext]
    entity_id: ClassVar[str]

    # class members: configure the entity component testing
    DOMAIN: str
    ENTITY_TYPE: ClassVar[EntityType]
    DIGEST_ENTITIES: ClassVar[dict[str, MerossEntityTypeSet]] = {}
    NAMESPACES_ENTITIES: ClassVar[dict[str, MerossEntityTypeSet]] = {}
    HUB_SUBDEVICES_ENTITIES: ClassVar[dict[str, MerossEntityTypeSet]] = {}

    async def async_service_call(
        self, service: str, service_data: dict = {}
    ):
        await self.hass_service_call(
            self.DOMAIN,
            service,
            service_data=service_data | {"entity_id": self.entity_id},
            blocking=True,
        )
        assert (state := self.hass_states.get(self.entity_id))
        return state

    async def async_test_each_callback(self, entity: MerossEntity):
        pass

    async def async_test_enabled_callback(self, entity: MerossEntity):
        pass

    async def async_test_disabled_callback(self, entity: MerossEntity):
        pass
