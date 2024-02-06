
from typing import Any, Awaitable, Callable, ClassVar, Coroutine, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import STATE_UNAVAILABLE, Entity

from custom_components.meross_lan.meross_entity import MerossEntity
from custom_components.meross_lan.merossclient import const as mc
from emulator import generate_emulators

from tests import const as tc, helpers

TEntity = TypeVar("TEntity", bound=Entity)


async def async_iterate_entities(
    hass: HomeAssistant,
    aioclient_mock,
    domain: str,
    entitytype: type[Entity],
    digest_class_map: dict[str, tuple],
    namespace_class_map: dict[str, tuple],
    hub_class_map: dict[str, tuple],
    async_test_each_callback,
    async_test_enabled_callback,
    async_test_disabled_callback,
):
    """
    - digest_class_map, namespace_class_map, hub_class_map: if any is not empty process only the devices
    matching the digest key or namespace ability else (all empty) process all of the device entities
    """
    for emulator in generate_emulators(
        tc.EMULATOR_TRACES_PATH, tc.MOCK_DEVICE_UUID, tc.MOCK_KEY
    ):
        descriptor = emulator.descriptor
        ability = descriptor.ability
        digest = descriptor.digest

        merossentitytypes = set()
        for key, def_tuple in digest_class_map.items():
            if key in digest:
                merossentitytypes.add(def_tuple[0])

        for namespace, def_tuple in namespace_class_map.items():
            if namespace in ability:
                merossentitytypes.add(def_tuple[0])

        if hub_class_map and mc.KEY_HUB in digest:
            for p_subdevice in digest[mc.KEY_HUB][mc.KEY_SUBDEVICE]:
                for p_key, p_value in p_subdevice.items():
                    if isinstance(p_value, dict):
                        if p_key in hub_class_map:
                            merossentitytypes.add(hub_class_map[p_key][0])
                        break

        if digest_class_map or namespace_class_map:
            if not merossentitytypes:
                continue

        async with helpers.DeviceContext(hass, emulator, aioclient_mock) as context:
            device = await context.perform_coldstart()
            entities = device.managed_entities(domain)
            for entity in entities:
                assert isinstance(entity, entitytype)
                entity_id = entity.entity_id
                await async_test_each_callback(entity)

                for merossentitytype in merossentitytypes:
                    if isinstance(entity, merossentitytype):
                        merossentitytypes.remove(merossentitytype)
                        break

                state = hass.states.get(entity_id)
                if state:
                    assert entity._hass_connected
                    if not entity.available:
                        # skip entities which are not available in emulator (warning though)
                        assert state.state == STATE_UNAVAILABLE
                        continue

                    await async_test_enabled_callback(entity, entity_id)

                else:
                    # entity not loaded in HA
                    assert not entity._hass_connected
                    if not entity.available:
                        continue

                    # just test the internal interface
                    await async_test_disabled_callback(entity)

            assert not merossentitytypes, f"device({device.descriptor.type}-{context.device_id}) does not generate {merossentitytypes}"