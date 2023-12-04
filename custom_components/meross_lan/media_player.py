from __future__ import annotations

import typing

from homeassistant.components.media_player import (
    DOMAIN as PLATFORM_MEDIA_PLAYER,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
)
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MUSIC,
    MediaPlayerEntityFeature,
)
from homeassistant.const import STATE_IDLE, STATE_PLAYING

from . import meross_entity as me
from .helpers import PollingStrategy, clamp
from .light import MLLight
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .light import LightMixin


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(
        hass, config_entry, async_add_devices, PLATFORM_MEDIA_PLAYER
    )


class MLMp3Player(me.MerossEntity, MediaPlayerEntity):
    PLATFORM = PLATFORM_MEDIA_PLAYER

    manager: Mp3Mixin
    __slots__ = ("_mp3",)

    def __init__(self, manager: Mp3Mixin, channel: object):
        super().__init__(manager, channel, mc.KEY_MP3, MediaPlayerDeviceClass.SPEAKER)
        self._mp3 = {}
        self._attr_supported_features = (
            MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_STEP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.STOP
        )  # type: ignore

    @property
    def volume_level(self):
        volume = self._mp3.get(mc.KEY_VOLUME)
        if volume is None:
            return None
        return clamp(volume / 16, 0.0, 1.0)

    @property
    def is_volume_muted(self) -> bool | None:
        return self._mp3.get(mc.KEY_MUTE)

    @property
    def media_content_type(self):
        return MEDIA_TYPE_MUSIC

    @property
    def media_title(self):
        track = self.media_track
        if track is None:
            return None
        return mc.HP110A_MP3_SONG_MAP.get(track)

    @property
    def media_track(self) -> int | None:
        return self._mp3.get(mc.KEY_SONG)

    async def async_mute_volume(self, mute):
        await self.async_request_mp3(mc.KEY_MUTE, 1 if mute else 0)

    async def async_set_volume_level(self, volume):
        await self.async_request_mp3(mc.KEY_VOLUME, clamp(int(volume * 16), 0, 16))

    async def async_media_play(self):
        await self.async_request_mp3(mc.KEY_MUTE, 0)

    async def async_media_stop(self):
        await self.async_request_mp3(mc.KEY_MUTE, 1)

    async def async_media_previous_track(self):
        song = self.media_track
        if song is None:
            song = mc.HP110A_MP3_SONG_MIN
        elif song <= mc.HP110A_MP3_SONG_MIN:
            song = mc.HP110A_MP3_SONG_MAX
        else:
            song = song - 1
        await self.async_request_mp3(mc.KEY_SONG, song)

    async def async_media_next_track(self):
        song = self.media_track
        if song is None:
            song = mc.HP110A_MP3_SONG_MIN
        elif song >= mc.HP110A_MP3_SONG_MAX:
            song = mc.HP110A_MP3_SONG_MIN
        else:
            song = song + 1
        await self.async_request_mp3(mc.KEY_SONG, song)

    async def async_request_mp3(self, key: str, value: int):
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_MP3,
            mc.METHOD_SET,
            {mc.KEY_MP3: {mc.KEY_CHANNEL: self.channel, key: value}},
        ):
            self._mp3[key] = value
            self._attr_state = (
                STATE_IDLE if self._mp3.get(mc.KEY_MUTE) else STATE_PLAYING
            )
            if self._hass_connected:
                self._async_write_ha_state()

    def _parse_mp3(self, payload: dict):
        """
        {"channel": 0, "lmTime": 1630691532, "song": 9, "mute": 1, "volume": 11}
        """
        if payload and ((self._mp3 != payload) or not self.available):
            self._mp3 = payload
            if mc.KEY_MUTE in payload:
                self._attr_state = STATE_IDLE if payload[mc.KEY_MUTE] else STATE_PLAYING
            if self._hass_connected:
                self._async_write_ha_state()


class Mp3Mixin(
    LightMixin if typing.TYPE_CHECKING else object
):  # pylint: disable=used-before-assignment
    def __init__(self, descriptor, entry):
        super().__init__(descriptor, entry)
        with self.exception_warning("Mp3Mixin init"):
            # looks like digest (in NS_ALL) doesn't carry state
            # so we're not implementing _init_xxx and _parse_xxx methods here
            MLMp3Player(self, 0)
            self.polling_dictionary[mc.NS_APPLIANCE_CONTROL_MP3] = PollingStrategy(
                mc.NS_APPLIANCE_CONTROL_MP3
            )
            # cherub light entity should be there...
            light: MLLight = self.entities.get(0)  # type: ignore
            if light:
                light.update_effect_map(mc.HP110A_LIGHT_EFFECT_MAP)

    def _handle_Appliance_Control_Mp3(self, header: dict, payload: dict):
        """
        {"mp3": {"channel": 0, "lmTime": 1630691532, "song": 9, "mute": 1, "volume": 11}}
        """
        self._parse__generic(mc.KEY_MP3, payload.get(mc.KEY_MP3), mc.KEY_MP3)
