from typing import TYPE_CHECKING

from homeassistant import const as hac
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity as haec

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)
from custom_components.meross_lan.switch import ToggleX

from tests.helpers import DeviceContext

if TYPE_CHECKING:
    from typing import Any, Callable, ClassVar

    from homeassistant.core import Context, ServiceResponse, State

    from custom_components.meross_lan.helpers import entity as mle
    from custom_components.meross_lan.merossclient.protocol import types as mt

    EntityType = type[haec.Entity]
    MerossEntityTypesList = list[type[mle.Entity]]
    type MerossEntityTypesDigestContainer = (
        MerossEntityTypesList | dict[str, MerossEntityTypesList]
    )
    DeviceEntitiesType = MerossEntityTypesList
    DigestEntitiesType = dict[str, MerossEntityTypesDigestContainer]
    NamespaceEntitiesType = dict[mn.Namespace, MerossEntityTypesList]
    HubSubDeviceEntitiesType = dict[str | None, MerossEntityTypesList]
    """Container mapping the expected entities for any specific subdevice type.
    None (in the map) means the entities list is expected for any device type (i.e. battery)."""


class EntityComponentTest:
    """
    Provides an interface for each entity domain to execute
    proper testing on the different test types.
    """

    if TYPE_CHECKING:
        # static test context
        hass: ClassVar[HomeAssistant]

        @staticmethod
        def get_hass_state(entity_id: str) -> State | None: ...

        @staticmethod
        async def async_hass_service_call(
            domain: str,
            service: str,
            service_data: dict[str, Any] | None = None,
            blocking: bool = False,
            context: Context | None = None,
            target: dict[str, Any] | None = None,
            return_response: bool = False,
        ) -> ServiceResponse: ...

        ability: ClassVar[mt.JsonMapping]
        digest: ClassVar[mt.system.All_Digest]
        expected_entity_types: ClassVar[MerossEntityTypesList]
        device_context: ClassVar[DeviceContext]
        entity_id: ClassVar[str]

        # class members: configure the entity component testing
        DOMAIN: str
        ENTITY_TYPE: ClassVar[EntityType]
        DEVICE_ENTITIES: ClassVar[DeviceEntitiesType]
        """Types of entities which are instanced on every device."""
        DIGEST_ENTITIES: ClassVar[DigestEntitiesType]
        """Types of entities which are instanced based off the digest structure."""
        NAMESPACES_ENTITIES: ClassVar[NamespaceEntitiesType]
        """Types of entities which are instanced based off namespace ability presence."""
        HUB_SUBDEVICES_ENTITIES: ClassVar[HubSubDeviceEntitiesType]
        """Types of entities which are instanced based off subdevice definition in Hub digest."""

    DEVICE_ENTITIES = []
    """Types of entities which are instanced on every device."""
    DIGEST_ENTITIES = {}
    """Types of entities which are instanced based off the digest structure."""
    NAMESPACES_ENTITIES = {}
    """Types of entities which are instanced based off namespace ability presence."""
    HUB_SUBDEVICES_ENTITIES = {}

    async def async_service_call(self, service: str, service_data: dict = {}):
        """Helper to assert execution of a service call and return the resulting state.
        The entity_id service data is automatically defaulted to the current entity,
        nevertheless it can be overridden by providing an 'entity_id' key in the service_data dict,
        so only the service specific data needs to be provided."""
        try:
            entity_id = service_data["entity_id"]
        except KeyError:
            entity_id = self.entity_id
            service_data = dict(service_data)
            service_data["entity_id"] = entity_id
        await self.async_hass_service_call(
            self.DOMAIN,
            service,
            service_data=service_data,
            blocking=True,
        )
        assert (state := EntityComponentTest.get_hass_state(entity_id)), (
            "missing state",
            entity_id,
        )
        return state

    async def async_service_response(self, service: str, service_data: dict = {}):
        return await self.async_hass_service_call(
            self.DOMAIN,
            service,
            service_data={"entity_id": self.entity_id} | service_data,
            blocking=True,
            return_response=True,
        )

    async def async_service_call_check(
        self, service: str, expected_state: str, service_data: dict = {}
    ):
        state = await self.async_service_call(service, service_data)
        assert (
            state.state == expected_state
        ), f"service:{service} - result:{state.state} - expected:{expected_state}"
        await self.device_context.async_poll_single()
        assert (state := EntityComponentTest.get_hass_state(self.entity_id)), (
            "missing state",
            self.entity_id,
        )
        assert (
            state.state == expected_state
        ), f"service:{service} - result:{state.state} - expected:{expected_state}"
        return state

    async def async_test_each_callback(self, entity: "mle.Entity"):
        # manager should be online so this should always be true
        assert entity.available, f"entity {entity.entity_id} not available"

    async def async_test_enabled_callback(self, entity: "mle.Entity"):
        pass

    async def async_test_disabled_callback(self, entity: "mle.Entity"):
        pass

    def _check_remove_togglex(self, entity: "mle.Entity"):
        """
        Use to remove expected (but not instantiated) ToggleXSwitch entities
        for those hybrid entities which overtake ToggleX behavior
        """
        for togglex_digest in self.digest.get(mc.KEY_TOGGLEX, []):
            if togglex_digest[mc.KEY_CHANNEL] == entity.channel:
                EntityComponentTest.expected_entity_types.remove(ToggleX)


class ToggleEntityComponentTest(EntityComponentTest):
    """Partial specialization for platforms derived from core ToggleEntity."""

    async def async_test_enabled_callback(self, entity: haec.ToggleEntity):
        await self.async_service_call_check(hac.SERVICE_TURN_ON, hac.STATE_ON)
        await self.async_service_call_check(hac.SERVICE_TURN_OFF, hac.STATE_OFF)

    async def async_test_disabled_callback(self, entity: haec.ToggleEntity):
        await entity.async_turn_on()
        assert entity.is_on
        await entity.async_turn_off()
        assert not entity.is_on
