from __future__ import annotations

import typing

from homeassistant.components import media_player
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)

from . import meross_entity as me
from .helpers import clamp
from .helpers.namespaces import PollingStrategy
from .light import MLLight
from .merossclient import const as mc  # mEROSS cONST

if typing.TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .meross_device import MerossDevice


async def async_setup_entry(
    hass: HomeAssistant, config_entry: ConfigEntry, async_add_devices
):
    me.platform_setup_entry(hass, config_entry, async_add_devices, media_player.DOMAIN)


class MLMp3Player(me.MerossEntity, media_player.MediaPlayerEntity):
    PLATFORM = media_player.DOMAIN

    manager: MerossDevice

    # HA core entity attributes:
    is_volume_muted: bool | None
    media_content_type: MediaType = MediaType.MUSIC
    media_title: str | None
    media_track: int | None
    state: media_player.MediaPlayerState | None
    supported_features: MediaPlayerEntityFeature = (
        MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
    )
    volume_level: float | None
    volume_step: float = 1 / mc.HP110A_MP3_VOLUME_MAX

    __slots__ = (
        "is_volume_muted",
        "media_title",
        "media_track",
        "state",
        "volume_level",
        "_mp3",
    )

    def __init__(self, manager: MerossDevice):
        self._mp3 = {}
        self.is_volume_muted = None
        self.media_title = None
        self.media_track = None
        self.state = None
        self.volume_level = None
        super().__init__(
            manager, 0, mc.KEY_MP3, media_player.MediaPlayerDeviceClass.SPEAKER
        )
        manager.register_parser(mc.NS_APPLIANCE_CONTROL_MP3, self)
        PollingStrategy(manager, mc.NS_APPLIANCE_CONTROL_MP3)
        # cherub light entity should be there...
        light: MLLight = manager.entities.get(0)  # type: ignore
        if light:
            light.update_effect_map(mc.HP110A_LIGHT_EFFECT_MAP)

    # interface: MerossEntity
    def set_unavailable(self):
        self._mp3 = {}
        self.is_volume_muted = None
        self.media_title = None
        self.media_track = None
        self.state = None
        self.volume_level = None
        super().set_unavailable()

    # interface: MediaPlayerEntity
    async def async_mute_volume(self, mute):
        await self.async_request_mp3(mc.KEY_MUTE, 1 if mute else 0)

    async def async_set_volume_level(self, volume):
        await self.async_request_mp3(mc.KEY_VOLUME, clamp(round(volume * mc.HP110A_MP3_VOLUME_MAX), 0, mc.HP110A_MP3_VOLUME_MAX))

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

    # interface: self
    async def async_request_mp3(self, key: str, value: int):
        payload = {mc.KEY_CHANNEL: self.channel, key: value}
        if await self.manager.async_request_ack(
            mc.NS_APPLIANCE_CONTROL_MP3,
            mc.METHOD_SET,
            {mc.KEY_MP3: payload},
        ):
            self._parse_mp3(payload)

    def _parse_mp3(self, payload: dict):
        """
        {"channel": 0, "lmTime": 1630691532, "song": 9, "mute": 1, "volume": 11}
        """
        if self._mp3 != payload:
            self._mp3.update(payload)
            if mc.KEY_MUTE in payload:
                self.is_volume_muted = mute = payload[mc.KEY_MUTE]
                self.state = MediaPlayerState.IDLE if mute else MediaPlayerState.PLAYING
            if mc.KEY_SONG in payload:
                self.media_track = song = payload[mc.KEY_SONG]
                self.media_title = mc.HP110A_MP3_SONG_MAP.get(song)
            if mc.KEY_VOLUME in payload:
                self.volume_level = clamp(payload[mc.KEY_VOLUME] / mc.HP110A_MP3_VOLUME_MAX, 0.0, 1.0)
            self.flush_state()

