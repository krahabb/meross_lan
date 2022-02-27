from __future__ import annotations
import imp

from homeassistant.components.media_player import (
    DOMAIN as PLATFORM_MEDIA_PLAYER,
    MediaPlayerEntity, MediaPlayerDeviceClass,
)
from homeassistant.components.media_player.const import (
    SUPPORT_TURN_ON, SUPPORT_TURN_OFF,
    SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET, SUPPORT_VOLUME_STEP,
    SUPPORT_NEXT_TRACK, SUPPORT_PREVIOUS_TRACK,
    SUPPORT_PAUSE, SUPPORT_PLAY, SUPPORT_STOP,
    MEDIA_TYPE_MUSIC,
)
from homeassistant.const import (
    STATE_IDLE, STATE_OFF, STATE_PLAYING,
)

from .merossclient import const as mc  # mEROSS cONST
from .meross_entity import (
    _MerossEntity,
    platform_setup_entry, platform_unload_entry,
)
from .light import MLLight, SUPPORT_EFFECT
from .helpers import LOGGER, clamp


async def async_setup_entry(hass: object, config_entry: object, async_add_devices):
    platform_setup_entry(hass, config_entry, async_add_devices, PLATFORM_MEDIA_PLAYER)

async def async_unload_entry(hass: object, config_entry: object) -> bool:
    return platform_unload_entry(hass, config_entry, PLATFORM_MEDIA_PLAYER)



class MLMp3Player(_MerossEntity, MediaPlayerEntity):

    PLATFORM = PLATFORM_MEDIA_PLAYER


    def __init__(self, device: 'MerossDevice', channel: object):
        super().__init__(device, channel, mc.KEY_MP3, MediaPlayerDeviceClass.SPEAKER)

        self._mp3 = dict()

        self._attr_supported_features = \
            SUPPORT_VOLUME_MUTE|SUPPORT_VOLUME_SET|SUPPORT_VOLUME_STEP|\
            SUPPORT_NEXT_TRACK|SUPPORT_PREVIOUS_TRACK|\
            SUPPORT_PLAY|SUPPORT_STOP
            #SUPPORT_TURN_ON|SUPPORT_TURN_OFF|\

        self._attr_media_content_type = MEDIA_TYPE_MUSIC


    @property
    def volume_level(self) -> float | None:
        volume = self._mp3.get(mc.KEY_VOLUME)
        if volume is None:
            return None
        return clamp(volume / 16, 0, 1)


    @property
    def is_volume_muted(self) -> bool | None:
        return self._mp3.get(mc.KEY_MUTE)


    @property
    def media_title(self) -> str | None:
        track = self.media_track
        if track is None:
            return None
        return mc.HP110A_MP3_SONG_MAP.get(track)


    @property
    def media_track(self) -> int | None:
        return self._mp3.get(mc.KEY_SONG)

    """
    async def async_turn_on(self):
        def _ack_callback():
            self.update_onoff(onoff)

        self.device.request(
            mc.NS_APPLIANCE_CONTROL_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 1}},
            _ack_callback)



    async def async_turn_off(self):
        def _ack_callback():
            self.update_state(STATE_OFF)

        self.device.request(
            mc.NS_APPLIANCE_CONTROL_TOGGLEX,
            mc.METHOD_SET,
            {mc.KEY_TOGGLEX: {mc.KEY_CHANNEL: self.channel, mc.KEY_ONOFF: 0}},
            _ack_callback)
    """


    async def async_mute_volume(self, mute):
        self._request_mp3(mc.KEY_MUTE, 1 if mute else 0)


    async def async_set_volume_level(self, volume):
        self._request_mp3(mc.KEY_VOLUME, int(clamp(volume * 16, 0, 16)))


    async def async_media_play(self):
        self._request_mp3(mc.KEY_MUTE, 0)


    async def async_media_stop(self):
        self._request_mp3(mc.KEY_MUTE, 1)


    async def async_media_previous_track(self):
        song = self.media_track
        if song is None:
            song = mc.HP110A_MP3_SONG_MIN
        elif song <= mc.HP110A_MP3_SONG_MIN:
            song = mc.HP110A_MP3_SONG_MAX
        else:
            song = song - 1
        self._request_mp3(mc.KEY_SONG, song)


    async def async_media_next_track(self):
        song = self.media_track
        if song is None:
            song = mc.HP110A_MP3_SONG_MIN
        elif song >= mc.HP110A_MP3_SONG_MAX:
            song = mc.HP110A_MP3_SONG_MIN
        else:
            song = song + 1
        self._request_mp3(mc.KEY_SONG, song)


    def _request_mp3(self, key, value):

        def _ack_callback():
            self._mp3[key] = value
            self._attr_state = STATE_IDLE if self._mp3.get(mc.KEY_MUTE) else STATE_PLAYING
            if self.hass and self.enabled:
                self.async_write_ha_state()

        self.device.request(
            mc.NS_APPLIANCE_CONTROL_MP3,
            mc.METHOD_SET,
            {
                mc.KEY_MP3:
                {
                    mc.KEY_CHANNEL: self.channel,
                    key: value
                }
            },
            _ack_callback)


    def _parse_mp3(self, payload: dict):
        """
        {"channel": 0, "lmTime": 1630691532, "song": 9, "mute": 1, "volume": 11}
        """
        if payload and ((self._mp3 != payload) or not self.available):
            self._mp3 = payload
            if mc.KEY_MUTE in payload:
                self._attr_state = STATE_IDLE if payload[mc.KEY_MUTE] else STATE_PLAYING
            if self.hass and self.enabled:
                self.async_write_ha_state()



class Mp3Mixin:


    def __init__(self, api, descriptor, entry):
        super().__init__(api, descriptor, entry)

        try:
            # looks like digest (in NS_ALL) doesn't carry state
            # so we're not implementing _init_xxx and _parse_xxx methods here
            MLMp3Player(self, 0)
            self.polling_dictionary.add(mc.NS_APPLIANCE_CONTROL_MP3)
            # cherub light entity should be there...
            light: MLLight = self.entities.get(0)
            if light is not None:
                light._light_effect_map = dict(mc.HP110A_LIGHT_EFFECT_MAP)
                light._attr_effect_list = list(mc.HP110A_LIGHT_EFFECT_MAP.values())
                light._attr_supported_features = light._attr_supported_features | SUPPORT_EFFECT
        except Exception as e:
            LOGGER.warning("Mp3Mixin(%s) init exception:(%s)", self.device_id, str(e))


    def _init_light(self, payload: dict):
        """
        This mixin is higher in inheritance chain than LightMixin
        so we're intercepting MLLight instantiation
        """
        super()._init_light(payload)



    def _handle_Appliance_Control_Mp3(self,
    namespace: str, method: str, payload: dict, header: dict):
        """
        {"mp3": {"channel": 0, "lmTime": 1630691532, "song": 9, "mute": 1, "volume": 11}}
        """
        self._parse__generic(mc.KEY_MP3, payload.get(mc.KEY_MP3), mc.KEY_MP3)