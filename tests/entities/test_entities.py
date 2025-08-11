from importlib import import_module
import re
from typing import TYPE_CHECKING

from homeassistant.helpers.entity import STATE_UNAVAILABLE

from custom_components.meross_lan.devices.hub import HubMixin
from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

from tests import const as tc, helpers
from tests.entities import EntityComponentTest

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest import CaptureFixture

    from custom_components.meross_lan.helpers.device import BaseDevice

    from tests.entities import (
        DeviceEntitiesType,
        DigestEntitiesType,
        HubSubDeviceEntitiesType,
        MerossEntityTypesList,
        NamespaceEntitiesType,
    )

    COMPONENTS_TESTS: dict[str, EntityComponentTest]
    DEVICE_ENTITIES: DeviceEntitiesType
    DIGEST_ENTITIES: DigestEntitiesType
    NAMESPACES_ENTITIES: NamespaceEntitiesType
    HUB_SUBDEVICES_ENTITIES: HubSubDeviceEntitiesType

COMPONENTS_TESTS = {}
DEVICE_ENTITIES = []
DIGEST_ENTITIES = {}
NAMESPACES_ENTITIES = {}
HUB_SUBDEVICES_ENTITIES = {None: []}

# list of exclusions from the general rule which states that
# every entity must be 'available' once the device is loaded
# this might be due to malformed traces which miss some info
# and so cannot properly 'online' the releated entity. Use with care!
UNAVAILABLE_ENTITIES = {}

for entity_domain in (
    "binary_sensor",
    "button",
    "calendar",
    "climate",
    "cover",
    "fan",
    "light",
    "media_player",
    "number",
    "select",
    "sensor",
    "switch",
):
    module = import_module(f".{entity_domain}", "tests.entities")
    entity_test: EntityComponentTest = module.EntityTest()
    entity_test.DOMAIN = entity_domain
    COMPONENTS_TESTS[entity_domain] = entity_test

    DEVICE_ENTITIES.extend(entity_test.DEVICE_ENTITIES)

    for digest_key, entity_types in entity_test.DIGEST_ENTITIES.items():
        # digest entity type description might be hiearchical
        # since digest iteslf might be a dict hierarchy (2 levels though)
        try:
            container = DIGEST_ENTITIES[digest_key]
            assert type(container) is type(entity_types)
            if isinstance(entity_types, dict):
                assert isinstance(container, dict)
                for sub_digest_key, sub_entity_types in entity_types.items():
                    sub_container = container.setdefault(sub_digest_key, [])
                    sub_container.extend(sub_entity_types)
            else:
                assert isinstance(container, list)
                container.extend(entity_types)
        except KeyError:
            DIGEST_ENTITIES[digest_key] = entity_types.copy()

    for namespace, entity_types in entity_test.NAMESPACES_ENTITIES.items():
        try:
            NAMESPACES_ENTITIES[namespace].extend(entity_types)
        except KeyError:
            NAMESPACES_ENTITIES[namespace] = list(entity_types)

    for subdevice_type, entity_types in entity_test.HUB_SUBDEVICES_ENTITIES.items():
        try:
            HUB_SUBDEVICES_ENTITIES[subdevice_type].extend(entity_types)
        except KeyError:
            HUB_SUBDEVICES_ENTITIES[subdevice_type] = list(entity_types)


async def test_entities(
    request,
    hass: "HomeAssistant",
    capsys: "CaptureFixture",
    disable_entity_registry_update,
):
    """
    tests basic (and complex sometimes) entities behavior when running/responding
    to actual HA service calls. We're looping through all of our emulator traces
    in order to try cover all of the entities features. For each entity platform
    the test code is defined in the respective module.
    """
    EntityComponentTest.hass = hass
    EntityComponentTest.hass_states = hass.states
    EntityComponentTest.hass_service_call = hass.services.async_call

    unexpected_entities_summary: dict[str, list[str]] = {}
    unavailable_entities_summary: dict[str, list[str]] = {}

    try:
        for emulator in helpers.build_emulators():

            descriptor = emulator.descriptor
            EntityComponentTest.ability = ability = descriptor.ability
            EntityComponentTest.digest = digest = descriptor.digest
            ishub = mc.KEY_HUB in digest

            EntityComponentTest.expected_entity_types = expected_entities = (
                DEVICE_ENTITIES.copy()
            )
            _add_func = expected_entities.extend
            for digest_key, entity_types in DIGEST_ENTITIES.items():
                if digest_key in digest:
                    sub_digest = digest[digest_key]
                    if isinstance(entity_types, list):
                        if isinstance(sub_digest, list):
                            for _ in sub_digest:
                                _add_func(entity_types)
                        else:
                            # this is somewhat specific for "light" digest key
                            # which doesn't carry a list but just a single channel
                            # dict struct
                            _add_func(entity_types)
                    else:  # digest carries a 2nd level
                        assert isinstance(sub_digest, dict)
                        for sub_digest_key, sub_entity_types in entity_types.items():
                            if sub_digest_key in sub_digest:
                                for channel_digest in sub_digest[sub_digest_key]:
                                    _add_func(sub_entity_types)

            for namespace, entity_types in NAMESPACES_ENTITIES.items():
                if namespace in ability:
                    _add_func(entity_types)
            if ishub:
                subdevice_ids = set()
                for p_subdevice in digest[mc.KEY_HUB][mc.KEY_SUBDEVICE]:
                    subdevice_id = p_subdevice[mc.KEY_ID]
                    if subdevice_id in subdevice_ids:
                        # get rid of duplicated ids in digest..they'll be
                        # discarded in Hub too
                        # (see trace msh300hk-01234567890123456789012345678916)
                        continue
                    # Add the expected entities common to any device type
                    _add_func(HUB_SUBDEVICES_ENTITIES[None])
                    # Record the id to check for duplicates
                    subdevice_ids.add(subdevice_id)
                    for p_key, p_value in p_subdevice.items():
                        if isinstance(p_value, dict):
                            if p_key in HUB_SUBDEVICES_ENTITIES:
                                _add_func(HUB_SUBDEVICES_ENTITIES[p_key])
                            break

            async with helpers.DeviceContext(request, hass, emulator) as device_context:
                EntityComponentTest.device_context = device_context
                try:
                    device_name = device_context.config_entry.title
                    with capsys.disabled():
                        print(f"\nTesting {device_name}")
                        print(
                            f"Expected entities: {[_entity_type.__name__ for _entity_type in expected_entities]}"
                        )
                    unexpected_entities: list[str] = []
                    unavailable_entities: list[str] = []
                    device = await device_context.perform_coldstart()
                    await _async_test_entities(
                        device,
                        expected_entities,
                        unexpected_entities,
                        unavailable_entities,
                    )
                    if ishub:
                        assert isinstance(device, HubMixin)
                        for subdevice in device.subdevices.values():
                            await _async_test_entities(
                                subdevice,
                                expected_entities,
                                unexpected_entities,
                                unavailable_entities,
                            )

                    if unexpected_entities:
                        unexpected_entities_summary[device_name] = unexpected_entities
                    if unavailable_entities:
                        unavailable_entities_summary[device_name] = unavailable_entities

                    assert (
                        not expected_entities
                    ), f"{device_name} does not generate {expected_entities}"

                    # This could be safely removed once we finish off immutability for PayloadType
                    assert (
                        not mn.PayloadType.LIST.value
                        and not mn.PayloadType.DICT.value
                        and (len(mn.PayloadType.LIST_C.value) == 1)
                    ), f"device({descriptor.type}-{descriptor.uuid}) corrupts const data (namespaces)"

                except BaseException as e:
                    e.args = (*e.args, EntityComponentTest.entity_id)
                    raise e
                finally:
                    EntityComponentTest.device_context = None  # type: ignore
                    EntityComponentTest.entity_id = ""
    finally:
        EntityComponentTest.hass = None  # type: ignore
        EntityComponentTest.hass_states = None  # type: ignore
        EntityComponentTest.hass_service_call = None  # type: ignore

    with capsys.disabled():
        print("\nUnexpected entities:")
        for device_name, unexpected_entities in unexpected_entities_summary.items():
            if unexpected_entities:
                print(
                    f"- {device_name}:\n{[_entity for _entity in unexpected_entities]}\n"
                )
        print("\nUnavailable entities:")
        for device_name, unavailable_entities in unavailable_entities_summary.items():
            if unavailable_entities:
                print(
                    f"- {device_name}:\n{[_entity for _entity in unavailable_entities]}\n"
                )


async def _async_test_entities(
    manager: "BaseDevice",
    expected_entities: "MerossEntityTypesList",
    unexpected_entities: list[str],
    unavailable_entities: list[str],
):
    for entity in manager.entities.values():

        entity_type = type(entity)

        if entity.PLATFORM not in COMPONENTS_TESTS:
            # TODO: add missing platform tests
            helpers.LOGGER.warning("Missing testing for platform %s", entity.PLATFORM)
            unexpected_entities.append(entity.logtag)
            continue

        EntityComponentTest.entity_id = entity_id = entity.entity_id

        entity_component_test = COMPONENTS_TESTS[entity.PLATFORM]
        assert isinstance(entity, entity_component_test.ENTITY_TYPE)

        # This will ensure the entity is 'available' as per an online device
        await entity_component_test.async_test_each_callback(entity)

        if entity_type in expected_entities:
            expected_entities.remove(entity_type)
        else:
            unexpected_entities.append(entity.logtag)

        state = EntityComponentTest.hass_states.get(entity_id)
        if state:
            assert entity.hass_connected
            if state.state == STATE_UNAVAILABLE:
                # state availability should be asserted in the future
                # since it's an indication of failure in polling
                # device state. Right now we have issues in parsing ms600
                # so we have to demote this to a warning in our test logs
                unavailable_entities.append(entity.logtag)
            await entity_component_test.async_test_enabled_callback(entity)
        else:
            # entity not loaded in HA
            assert not entity.hass_connected
            assert not entity.enabled
            # assert not entity.entity_registry_enabled_default
            # just test the internal interface
            await entity_component_test.async_test_disabled_callback(entity)

    EntityComponentTest.entity_id = ""
