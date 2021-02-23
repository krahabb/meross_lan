"""Switch platform for integration_blueprint."""
from homeassistant.components.switch import SwitchEntity

from .const import DEFAULT_NAME, DOMAIN, ICON, SWITCH
from .entity import IntegrationBlueprintEntity


async def async_setup_entry(hass, entry, async_add_devices):
    """Setup sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices([IntegrationBlueprintBinarySwitch(coordinator, entry)])


class IntegrationBlueprintBinarySwitch(IntegrationBlueprintEntity, SwitchEntity):
    """integration_blueprint switch class."""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on the switch."""
        await self.coordinator.api.async_set_title("bar")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off the switch."""
        await self.coordinator.api.async_set_title("foo")
        await self.coordinator.async_request_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return f"{DEFAULT_NAME}_{SWITCH}"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return ICON

    @property
    def is_on(self):
        """Return true if the switch is on."""
        return self.coordinator.data.get("title", "") == "foo"
