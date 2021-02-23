![GitHub last commit](https://img.shields.io/github/last-commit/krahabb/meross_lan?style=for-the-badge)
[![GitHub](https://img.shields.io/github/license/krahabb/meross_lan?style=for-the-badge)](LICENCE)
[![hacs][hacsbadge]][hacs]


# Meross LAN
This [homeassistant](https://www.home-assistant.io/) integration allows you to control you *Meross* plugs all over your LAN without any need for cloud connectivity. It works through your own MQTT broker (or any other configured through the homeassistant mqtt integration).
In order for this to work you need to bind your *Meross* appliances to this same MQTT broker. Follow the guide at https://github.com/bytespider/Meross/wiki/MQTT to re-configure your devices and start integrating them locally from the HA Integrations page

HAVE FUN! 😎

## Installation
If you have [HACS](https://hacs.xyz) this is as simple as installing a custom component through the UI:
In your HA frontend go to HACS -> Integrations tap on the menu on the top-right corner and select 'Custom repositories'
Here select the 'Category' (Integration) and type/paste this repository url: https://github.com/krahabb/meross_lan
You'll have to restart HA to let it recognize the new integration

You can also install it manually if you don't have and/or don't want to use HACS.
Download and copy the 'custom_components/meross_lan' directory into the 'custom_components' folder on your homeassistant installation.
Depending on the type of HA installation you might have to follow specific instructions.
This is working for a standard 'core' installation but should work for any other flavour: remember to set the appropriate ownership and access rights on your copied files so the homeassistant user running your instance is able to read and execute the integration code.
Restart HA to let it play


## Setup
Be sure your *Meross* plugs are correctly connected to the mqtt broker by checking they are effectively publishing state updates. The best test here is to enter the mqtt integration configuration in HA and subscribe to all topics to see if your HA instance is receiving the relevant messages by entering the wildcard '#' topic.
If the '#' wildcard is too *wild* because you have a crowded mqtt infrastructure you can enter '/appliance/#' as a more specific topic wildcard since *Meross* devices will publish to a subdomain of this one. Manually switch your plug and check if you received any message in your mqtt configuration pane. The topic should be something in the form 'appliance/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/publish/' where the XXX... are the device identifier which is unique for every appliance.
Once you're setup with mqtt integration and device pairing you can got to your homeassistant Configuration -> Integrations and add the Meross LAN by looking through the list of available ones.
When you add it the first time, it will setup a 'software hub' (you will see the 'MQTT Hub' name on the configured integration) for the devices so it can start listening for MQTT messages from your Meross devices.
Now you can start adding devices by manually toggling them so they *push* a status message to the broker and then to HA: you will receive a notification for a discovered integration and you will be able to set it up by clicking 'Configure' on the related panel in the Integrations page. Confirm and you are all done. If everything goes ok the component will be able to auto detect the device and nothing need to be done. If your device is not discovered then it is probably my fault since I currently do not have many of them to test


## Supported hardware
At the moment this software has been developed and tested on the Meross MSS310R plug (power meter included). I have tried to make it the more optimistic and generalistic as possible based on the work from [@albertogeniola] and [@bytespider] so it should work with most of the plugs out there but..I did not test anything other than my MSS310Rs (firmware 2.1.4)

- [MSS310R](https://www.meross.com/product/38/article/) : power plug with metering capabilties


## References
This integration has been made possible only with the contribution of the awesome work done by:
- [@albertogeniola]
- [@bytespider]

Have a look at their repositories to better understand how the Meross line of devices is working through MQTT
I really thank them for the inspiration and the knowledge that made it possible for me to develop this integration

[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[@albertogeniola]: https://github.com/albertogeniola/MerossIot
[@bytespider]: https://github.com/bytespider/Meross
