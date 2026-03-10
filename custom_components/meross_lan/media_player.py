from typing import TYPE_CHECKING

from homeassistant.components import media_player
from homeassistant.components.media_player.const import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)

from .helpers import clamp
from .helpers.entity import ParserEntity
from .merossclient.protocol import const as mc, namespaces as mn

if TYPE_CHECKING:
    from typing import ClassVar, Final, NotRequired

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .helpers.device import Device
    from .helpers.entity import ChannelType
    from .merossclient.protocol.types import JsonDict


async def async_setup_entry(
    hass: "HomeAssistant", config_entry: "ConfigEntry", async_add_devices
):
    ParserEntity.platform_setup_entry(
        hass, config_entry, async_add_devices, media_player.DOMAIN
    )


class Mp3Player(ParserEntity, media_player.MediaPlayerEntity):

    if TYPE_CHECKING:

        parent: Final[Device]  # type: ignore[override]
        # HA core entity attributes:
        is_volume_muted: bool | None
        media_title: str | None
        media_track: int | None
        state: media_player.MediaPlayerState | None
        volume_level: float | None

    PLATFORM = media_player.DOMAIN
    ENTITY_KEY = mc.KEY_MP3
    ns = mn.Appliance_Control_Mp3

    # HA core entity attributes:
    _attr_device_class = media_player.MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
    )
    _attr_volume_step = 1 / mc.HP110A_MP3_VOLUME_MAX

    __slots__ = (
        "is_volume_muted",
        "media_title",
        "media_track",
        "state",
        "volume_level",
    )

    def __init__(self, channel: "ChannelType", device: "Device", /, **kwargs):
        self.is_volume_muted = None
        self.media_title = None
        self.media_track = None
        self.state = None
        self.volume_level = None
        super().__init__(channel, device, **kwargs)
        device.register_parser_entity(self)

    def set_unavailable(self):
        self.is_volume_muted = None
        self.media_title = None
        self.media_track = None
        self.state = None
        self.volume_level = None
        super().set_unavailable()

    # interface: MediaPlayerEntity
    async def async_mute_volume(self, mute):
        await self.async_request_parse_ex({mc.KEY_MUTE: 1 if mute else 0})

    async def async_set_volume_level(self, volume):
        await self.async_request_parse_ex(
            {
                mc.KEY_VOLUME: clamp(
                    round(volume * mc.HP110A_MP3_VOLUME_MAX),
                    0,
                    mc.HP110A_MP3_VOLUME_MAX,
                ),
            }
        )

    async def async_media_play(self):
        await self.async_request_parse_ex({mc.KEY_MUTE: 0})

    async def async_media_stop(self):
        await self.async_request_parse_ex({mc.KEY_MUTE: 1})

    async def async_media_previous_track(self):
        song = self.media_track
        await self.async_request_parse_ex(
            {
                mc.KEY_SONG: (
                    mc.HP110A_MP3_SONG_MAX
                    if (song is None) or (song <= mc.HP110A_MP3_SONG_MIN)
                    else song - 1
                ),
            }
        )

    async def async_media_next_track(self):
        song = self.media_track
        await self.async_request_parse_ex(
            {
                mc.KEY_SONG: (
                    mc.HP110A_MP3_SONG_MIN
                    if (song is None) or (song >= mc.HP110A_MP3_SONG_MAX)
                    else song + 1
                ),
            }
        )

    def _parse_mp3(self, payload: dict, /):
        """
        {"channel": 0, "lmTime": 1630691532, "song": 9, "mute": 1, "volume": 11}
        """
        if self.ns_payload != payload:
            self.ns_payload = payload
            if mc.KEY_MUTE in payload:
                self.is_volume_muted = mute = payload[mc.KEY_MUTE]
                self.state = MediaPlayerState.IDLE if mute else MediaPlayerState.PLAYING
            if mc.KEY_SONG in payload:
                self.media_track = song = payload[mc.KEY_SONG]
                self.media_title = mc.HP110A_MP3_SONG_MAP.get(song)
            if mc.KEY_VOLUME in payload:
                self.volume_level = clamp(
                    payload[mc.KEY_VOLUME] / mc.HP110A_MP3_VOLUME_MAX, 0.0, 1.0
                )
            self.flush_state()
