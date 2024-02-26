from homeassistant.components import media_player as haec  # HA EntityComponent
from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerState

from custom_components.meross_lan.media_player import MLMp3Player
from custom_components.meross_lan.merossclient import const as mc

from tests.entities import EntityComponentTest


class EntityTest(EntityComponentTest):

    ENTITY_TYPE = MediaPlayerEntity

    DIGEST_ENTITIES = {}

    NAMESPACES_ENTITIES = {
        mc.NS_APPLIANCE_CONTROL_MP3: [MLMp3Player],
    }

    SERVICE_STATE_MAP = {
        haec.SERVICE_MEDIA_PLAY: MediaPlayerState.PLAYING,
        haec.SERVICE_MEDIA_NEXT_TRACK: MediaPlayerState.PLAYING,
        haec.SERVICE_MEDIA_PREVIOUS_TRACK: MediaPlayerState.PLAYING,
        haec.SERVICE_MEDIA_STOP: MediaPlayerState.IDLE,
    }

    async def async_test_each_callback(self, entity: MLMp3Player):
        pass

    async def async_test_enabled_callback(self, entity: MLMp3Player):
        for service_name, expected_state in EntityTest.SERVICE_STATE_MAP.items():
            await self.async_service_call_check(service_name, expected_state)
        await self.async_service_call_check(
            haec.SERVICE_VOLUME_MUTE,
            MediaPlayerState.PLAYING,
            {haec.ATTR_MEDIA_VOLUME_MUTED: False},
        )
        state = await self.async_service_call_check(
            haec.SERVICE_VOLUME_SET,
            MediaPlayerState.PLAYING,
            {haec.ATTR_MEDIA_VOLUME_LEVEL: 1},
        )
        assert (
            state.attributes[haec.ATTR_MEDIA_VOLUME_LEVEL] == 1
        ), haec.ATTR_MEDIA_VOLUME_LEVEL
        state = await self.async_service_call_check(
            haec.SERVICE_VOLUME_SET,
            MediaPlayerState.PLAYING,
            {haec.ATTR_MEDIA_VOLUME_LEVEL: 0.1},
        )
        assert (
            state.attributes[haec.ATTR_MEDIA_VOLUME_LEVEL] == round(0.1 * 16) / 16
        ), haec.ATTR_MEDIA_VOLUME_LEVEL

    async def async_test_disabled_callback(self, entity: MLMp3Player):
        pass
