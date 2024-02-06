from importlib import import_module
from typing import Iterable

from homeassistant.core import HomeAssistant

from custom_components.meross_lan.meross_device_hub import MerossDeviceHub
from custom_components.meross_lan.meross_entity import MerossEntity
from custom_components.meross_lan.merossclient import const as mc
from emulator import generate_emulators

from tests import const as tc, helpers
from tests.entities import EntityComponentTest, EntityTestContext, MerossEntityTypeSet

COMPONENTS_TESTS: dict[str, EntityComponentTest] = {}
DIGEST_ENTITIES: dict[str, MerossEntityTypeSet] = {}
NAMESPACES_ENTITIES: dict[str, MerossEntityTypeSet] = {}
HUB_SUBDEVICES_ENTITIES: dict[str, MerossEntityTypeSet] = {}

for entity_domain in ("climate", "cover", "light", "number", "select", "switch"):
    module = import_module(f".{entity_domain}", "tests.entities")
    entity_test: EntityComponentTest = module.EntityTest()
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
    context = EntityTestContext()
    context.hass = hass

    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        context.ability = ability = descriptor.ability
        digest = descriptor.digest
        ishub = mc.KEY_HUB in digest

        context.expected_entity_types = expected_entity_types = set()
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
                context.device_context = device_context
                context.device = device = await device_context.perform_coldstart()
                await _async_test_entities(context, device.entities.values())
                if ishub:
                    assert isinstance(device, MerossDeviceHub)
                    for subdevice in device.subdevices.values():
                        context.subdevice = subdevice
                        await _async_test_entities(context, subdevice.entities.values())

                assert (
                    not expected_entity_types
                ), f"device({descriptor.type}-{descriptor.uuid}) does not generate {expected_entity_types}"

            except BaseException as e:
                e.args = (*e.args, context.entity_id)
                raise e
            finally:
                context.device_context = None  # type: ignore
                context.device = None  # type: ignore
                context.subdevice = None
                context.entity_id = None


async def _async_test_entities(
    context: EntityTestContext,
    entities: Iterable[MerossEntity],
):
    for entity in entities:
        if entity.PLATFORM not in COMPONENTS_TESTS:
            # TODO: add missing platform tests
            continue
        context.entity_id = entity_id = entity.entity_id
        entity_type = type(entity)
        if entity_type in context.expected_entity_types:
            context.expected_entity_types.remove(entity_type)

        entity_component_test = COMPONENTS_TESTS[entity.PLATFORM]
        assert isinstance(entity, entity_component_test.ENTITY_TYPE)
        assert entity.available, f"entity {entity_id} not available"
        await entity_component_test.async_test_each_callback(context, entity)

        state = context.hass.states.get(entity_id)
        if state:
            assert entity._hass_connected
            await entity_component_test.async_test_enabled_callback(
                context, entity, entity_id
            )
        else:
            # entity not loaded in HA
            assert not entity._hass_connected
            # just test the internal interface
            await entity_component_test.async_test_disabled_callback(context, entity)

    context.entity_id = None
