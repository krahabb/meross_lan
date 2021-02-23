[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]

# Meross LAN

This [homeassistant](https://www.home-assistant.io/) integration allows you to control you Meross plugs all over your LAN without any need for cloud connectivity. It works through your own MQTT broker (or any other configured through the homeassistant mqtt integration).  
In order for this to work you need to bind your Meross appliances to this same MQTT broker. Follow the guide at https://github.com/bytespider/Meross/wiki/MQTT to re-configure your devices and start integrating them locally from the HA Integrations page  

HAVE FUN! 😎

## Installation

If you have [HACS](https://hacs.xyz) this is as simple as installing a custom component through the UI:  
In your HA frontend go to HACS -> Integrations tap on the menu on the top-right corner and select 'Custom repositories'  
Here select the Category ('Integration') and type/paste this repository url: https://github.com/krahabb/meross_lan  
You'll have to restart HA to let it recognize the new integration

You can also install it manually if you don't have and/or don't want to use HACS.  
Download and copy the ''custom_components/meross_lan'' directory into the custom_components folder on your homeassistant installation.  
Depending on the type of HA installation you might have to follow specific instructions.  
This is working for a standard 'core' installation but should work for any other flavour: remember to set the appropriate ownership and access rights on your copied files so the homeassistant user running your instance is able to read and execute the integration code.  
Restart HA to let it play


## Setup

Be sure your Meross plugs are correctly connected to the mqtt broker by checking they are effectively publishing state updates. The best test here is to enter the mqtt integration configuration in HA and subscribe to all topics to see if your HA instance is receiving the relevant messages by entering the wildcard '#' topic
If the '#' wildcard is too wild because you have a crowded mqtt infrastructure you can enter '/appliance/#' as a more specific topic wildcard since Meross devices will publish to a subdomain of this topic. Manually switch your plug and check if you received any message in your mqtt configuration pane. The topic should be samoething in the form 'appliance/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/publish/' where the XXX... are the device identifier which is unique for every appliance






[integration_blueprint]: https://github.com/custom-components/integration_blueprint
[buymecoffee]: https://www.buymeacoffee.com/ludeeus
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/custom-components/blueprint.svg?style=for-the-badge
[commits]: https://github.com/custom-components/integration_blueprint/commits/master
[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[discord]: https://discord.gg/Qa5fW2R
[discord-shield]: https://img.shields.io/discord/330944238910963714.svg?style=for-the-badge
[exampleimg]: example.png
[forum-shield]: https://img.shields.io/badge/community-forum-brightgreen.svg?style=for-the-badge
[forum]: https://community.home-assistant.io/
[license-shield]: https://img.shields.io/github/license/custom-components/blueprint.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-Joakim%20Sørensen%20%40ludeeus-blue.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/custom-components/blueprint.svg?style=for-the-badge
[releases]: https://github.com/custom-components/integration_blueprint/releases
