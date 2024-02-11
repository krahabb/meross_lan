from importlib import import_module
import re
from typing import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import STATE_UNAVAILABLE

from custom_components.meross_lan.meross_device_hub import MerossDeviceHub
from custom_components.meross_lan.meross_entity import MerossEntity
from custom_components.meross_lan.merossclient import const as mc
from emulator import generate_emulators

from tests import const as tc, helpers
from tests.entities import EntityComponentTest, MerossEntityTypeSet

COMPONENTS_TESTS: dict[str, EntityComponentTest] = {}
DIGEST_ENTITIES: dict[str, MerossEntityTypeSet] = {}
NAMESPACES_ENTITIES: dict[str, MerossEntityTypeSet] = {}
HUB_SUBDEVICES_ENTITIES: dict[str, MerossEntityTypeSet] = {}

# list of exclusions from the general rule which states that
# every entity must be 'available' once the device is loaded
# this might be due to malformed traces which miss some info
# and so cannot properly 'online' the releated entity
UNAVAILABLE_ENTITIES = {
    r"calendar.*",  # a lot of mts schedules are not available in traces so far...
}

for entity_domain in (
    "calendar",
    "climate",
    "cover",
    "light",
    "media_player",
    "number",
    "select",
    "switch",
):
    module = import_module(f".{entity_domain}", "tests.entities")
    entity_test: EntityComponentTest = module.EntityTest()
    entity_test.DOMAIN = entity_domain
    COMPONENTS_TESTS[entity_domain] = entity_test
    for digest_key, entity_types in entity_test.DIGEST_ENTITIES.items():
        digest_set = DIGEST_ENTITIES.setdefault(digest_key, set())
        digest_set.update(entity_types)
    for namespace, entity_types in entity_test.NAMESPACES_ENTITIES.items():
        namespace_set = NAMESPACES_ENTITIES.setdefault(namespace, set())
        namespace_set.update(entity_types)
    for subdevice_type, entity_types in entity_test.HUB_SUBDEVICES_ENTITIES.items():
        subdevice_set = HUB_SUBDEVICES_ENTITIES.setdefault(subdevice_type, set())
        subdevice_set.update(entity_types)


async def test_entities(hass: HomeAssistant, aioclient_mock):
    """
    - digest_class_map, namespace_class_map, hub_class_map: if any is not empty process only the devices
    matching the digest key or namespace ability else (all empty) process all of the device entities
    """
    EntityComponentTest.hass = hass
    EntityComponentTest.hass_states = hass.states
    EntityComponentTest.hass_service_call = hass.services.async_call

    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        EntityComponentTest.ability = ability = descriptor.ability
        EntityComponentTest.digest = digest = descriptor.digest
        ishub = mc.KEY_HUB in digest

        EntityComponentTest.expected_entity_types = expected_entity_types = set()
        for digest_key, entity_types in DIGEST_ENTITIES.items():
            if digest_key in digest:
                expected_entity_types.update(entity_types)
        for namespace, entity_types in NAMESPACES_ENTITIES.items():
            if namespace in ability:
                expected_entity_types.update(entity_types)
        if ishub:
            for p_subdevice in digest[mc.KEY_HUB][mc.KEY_SUBDEVICE]:
                for p_key, p_value in p_subdevice.items():
                    if isinstance(p_value, dict):
                        if p_key in HUB_SUBDEVICES_ENTITIES:
                            expected_entity_types.update(HUB_SUBDEVICES_ENTITIES[p_key])
                        break

        async with helpers.DeviceContext(
            hass, emulator, aioclient_mock
        ) as device_context:
            try:
                EntityComponentTest.device_context = device_context
                device = await device_context.perform_coldstart()
                await _async_test_entities(device.entities.values())
                if ishub:
                    assert isinstance(device, MerossDeviceHub)
                    for subdevice in device.subdevices.values():
                        await _async_test_entities(subdevice.entities.values())

                assert (
                    not expected_entity_types
                ), f"device({descriptor.type}-{descriptor.uuid}) does not generate {expected_entity_types}"

            except BaseException as e:
                e.args = (*e.args, EntityComponentTest.entity_id)
                raise e
            finally:
                EntityComponentTest.device_context = None  # type: ignore
                EntityComponentTest.entity_id = ""


async def _async_test_entities(
    entities: Iterable[MerossEntity],
):
    for entity in entities:
        if entity.PLATFORM not in COMPONENTS_TESTS:
            # TODO: add missing platform tests
            continue
        EntityComponentTest.entity_id = entity_id = entity.entity_id
        entity_type = type(entity)
        if entity_type in EntityComponentTest.expected_entity_types:
            EntityComponentTest.expected_entity_types.remove(entity_type)

        entity_component_test = COMPONENTS_TESTS[entity.PLATFORM]
        assert isinstance(entity, entity_component_test.ENTITY_TYPE)

        await entity_component_test.async_test_each_callback(entity)

        for pattern in UNAVAILABLE_ENTITIES:
            if re.match(pattern, entity_id):
                break
        else:
            assert entity.available, f"entity {entity_id} not available"

        state = EntityComponentTest.hass_states.get(entity_id)
        if state:
            assert entity._hass_connected
            if not entity.available:
                assert state.state == STATE_UNAVAILABLE
                continue

            await entity_component_test.async_test_enabled_callback(entity)
        else:
            # entity not loaded in HA
            assert not entity._hass_connected
            if not entity.available:
                continue
            # just test the internal interface
            await entity_component_test.async_test_disabled_callback(entity)

    EntityComponentTest.entity_id = ""
