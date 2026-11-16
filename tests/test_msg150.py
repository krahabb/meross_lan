"""MSG150 garage door: Security+ signal validity and obstruction."""

from homeassistant.core import HomeAssistant

from custom_components.meross_lan.devices.garagedoor import GarageDoor
from custom_components.meross_lan.merossclient.protocol import const as mc
from emulator import build_emulator

from tests import const as tc, helpers

_MSG100_TRACE = (
    tc.EMULATOR_TRACES_PATH
    + "U01234567890123456789012345678906-Kpippo-msg100-1637650619.csv"
)


def _state(
    *,
    open_: int = 0,
    signal: int = mc.GARAGE_SIGNAL_VALID,
    obstruction: int = mc.GARAGE_OBSTRUCTION_CLEAR,
):
    return {
        mc.KEY_CHANNEL: 0,
        mc.KEY_OPEN: open_,
        mc.KEY_LMTIME: 0,
        mc.KEY_SIGNALVALID: signal,
        mc.KEY_OBSTRUCTION: obstruction,
    }


def _build_msg150():
    device = build_emulator(_MSG100_TRACE, key=tc.MOCK_KEY, uuid=tc.MOCK_DEVICE_UUID)
    device.descriptor.hardware[mc.KEY_TYPE] = mc.TYPE_MSG150
    channel = device.descriptor.digest[mc.KEY_GARAGEDOOR][0]
    channel[mc.KEY_OPEN] = 0
    channel[mc.KEY_SIGNALVALID] = mc.GARAGE_SIGNAL_VALID
    channel[mc.KEY_OBSTRUCTION] = mc.GARAGE_OBSTRUCTION_CLEAR
    return device


async def test_msg150_signal_and_obstruction(request, hass: HomeAssistant):
    """Valid signal exposes open/obstruction; an invalid signal does not."""
    emulator = _build_msg150()
    async with helpers.DeviceContext(
        request, hass, emulator, auto_poll=True
    ) as device_context:
        door = next(
            entity
            for entity in device_context.device.entities_iterable
            if isinstance(entity, GarageDoor)
        )
        signal = door.binary_sensor_signal
        assert signal is not None
        assert door.is_closed is True
        assert signal.is_on is True
        assert door.extra_state_attributes[GarageDoor.ATTR_GARAGE_SIGNAL_VALID] is True
        assert (
            door.extra_state_attributes[GarageDoor.ATTR_OBSTRUCTION_DETECTED] is False
        )

        door(
            _state(
                open_=0,
                obstruction=mc.GARAGE_OBSTRUCTION_DETECTED,
            )
        )
        assert door.is_closed is True
        assert (
            door.extra_state_attributes[GarageDoor.ATTR_OBSTRUCTION_DETECTED] is True
        )

        door.is_opening = True
        door._transition_start = door.parent.last_rx_epoch
        door(
            _state(
                open_=1,
                signal=mc.GARAGE_SIGNAL_INVALID,
                obstruction=mc.GARAGE_OBSTRUCTION_DETECTED,
            )
        )
        assert door.is_closed is True
        assert door.is_opening is False
        assert door._transition_start == 0.0
        assert signal.is_on is False
        assert door.extra_state_attributes[GarageDoor.ATTR_GARAGE_SIGNAL_VALID] is False
        assert GarageDoor.ATTR_OBSTRUCTION_DETECTED not in door.extra_state_attributes

        channel = emulator.descriptor.digest[mc.KEY_GARAGEDOOR][0]
        channel[mc.KEY_OPEN] = 0
        channel[mc.KEY_SIGNALVALID] = mc.GARAGE_SIGNAL_INVALID
        await door.async_open_cover()
        assert door.is_closed is True
        assert door.is_opening is not True

        door(
            _state(
                open_=1,
                signal=mc.GARAGE_SIGNAL_VALID,
                obstruction=mc.GARAGE_OBSTRUCTION_DETECTED,
            )
        )
        assert door.is_closed is False
        assert signal.is_on is True
        assert door.extra_state_attributes[GarageDoor.ATTR_GARAGE_SIGNAL_VALID] is True
        assert (
            door.extra_state_attributes[GarageDoor.ATTR_OBSTRUCTION_DETECTED] is True
        )
