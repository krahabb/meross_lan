![GitHub last commit](https://img.shields.io/github/last-commit/krahabb/meross_lan?style=for-the-badge)
[![GitHub](https://img.shields.io/github/license/krahabb/meross_lan?style=for-the-badge)](LICENCE)
[![hacs][hacsbadge]][hacs]


# Meross LAN

This [homeassistant](https://www.home-assistant.io/) integration allows you to control your *Meross* plugs all over your LAN without any need for cloud connectivity. It works through your own MQTT broker (or any other configured through the homeassistant mqtt integration).

In order for this to work you need to bind your *Meross* appliances to this same MQTT broker. Follow the guide at https://github.com/bytespider/Meross/wiki/MQTT to re-configure your devices and start integrating them locally from the HA Integrations page

HAVE FUN! 😎

## Installation

### HACS

In your HA frontend go to `HACS -> Integrations`, tap on the menu on the top-right corner and select `Custom repositories`.
Then select the `Category` (Integration) and type/paste this repository url: `https://github.com/krahabb/meross_lan`.

You'll have to restart HA to let it recognize the new integration

### Manual installation

Download and copy the `custom_components/meross_lan` directory into the `custom_components` folder on your homeassistant installation.

Depending on the type of HA installation you might have to follow specific instructions.

This is working for a standard 'core' installation but should work for any other flavour: remember to set the appropriate ownership and access rights on your copied files so the homeassistant user running your instance is able to read and execute the integration code.

Restart HA to let it play

## Setup

Make sure your *Meross* plugs are correctly connected to the MQTT broker by checking they are effectively publishing state updates. 

The best test here is to enter the mqtt integration configuration in HA and subscribe to all topics to see if your HA instance is receiving the relevant messages by listening to the `/appliance/#` topic since *Meross* devices will publish to a subdomain of this one. 

Manually switch your plug and check if you received any message in your MQTT configuration pane. The topic should be something in the form `appliance/XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX/publish/` where the `XXX...` is the device identifier which is unique for every appliance.

Once you are set up with the MQTT integration and device pairing, you can go to your homeassistant `Configuration -> Integrations` and add the Meross LAN by looking through the list of available ones.

When you add it the first time, it will setup a 'software hub' (you will see the 'MQTT Hub' name on the configured integration) for the devices so it can start listening for MQTT messages from your Meross devices. If you configured your device to use a *key* different than *''* you can configure the 'MQTT Hub' accordingly by setting a key in the 'Options' pane for the configuration entry of the integration

You can now start adding devices by manually toggling them so they *push* a status message to the broker and then to HA: you will receive a notification for a discovered integration and you will be able to set it up by clicking 'Configure' on the related panel in the Integrations page. Confirm and you are all done. In case of bulbs just plug/unplug them so they'll broadcast some status to the mqtt when my hub is listening

If everything goes the way should, the component will be able to auto detect the device and nothing need to be done. The optional device *key* you configured for the hub will be propagated to the discovered entry and 'fixed' in it's own configuration so you can eventually manage to change the key when discovering other appliances

If your device is not discovered then it is probably my fault since I currently do not have many of them to test


## Supported hardware

At the moment this software has been developed and tested on the Meross MSS310 plug and MSL100 bulb. I have tried to make it the more optimistic and generalistic as possible based on the work from [@albertogeniola] and [@bytespider] so it should work with most of the plugs out there but I did not test anything other than mines

- Switches
  - [MSS310R](https://www.meross.com/product/38/article/): power plug with metering capabilties
  - [MSS425](https://www.meross.com/product/16/article/): Smart WiFi Surge Protector (multiple sockets power strip)
- Lights
  - [MSL100R](https://www.meross.com/product/4/article/): Smart bulb with dimmable light
- Covers
  - Support for garage door opener and roller shutter is implemented by guess-work so I'm not expecting flawless behaviour but...could work


## Features

The component exposes the basic functionality of the underlying device (toggle on/off, dimm, report consumption through sensors) without any other effort, It should be able to detect if the device goes offline suddenly by using a periodic heartbeat on the mqtt channel (actually 5 min). The detection works faster for plugs with power metering since they're also polled every 30 second or so for the power values.


## Service

There is a service (since version 0.0.4) exposed to simplify communication with the device and play with it a bit. It basically requires the needed informations to setup a command request and send it over MQTT without the hassle of signatures and timestamps computations. You can check it in the 'Developer Tools' of the HA instance, everything should be enough self-explanatory there. 
I find it a bit frustrating that the HA service infrastructure does not allow to return anything from a service invocation so, the eventual reply from the device will get 'lost' in the mqtt flow. I've personally played a bit with the MQTT integration configuration pane to listen and see the mqtt responses from my devices but it's somewhat a pain unless you have a big screen to play with (or multiple monitors for the matter). Nevertheless you can use the service wherever you like to maybe invoke features at the device level or dig into it's configuration


## References

This integration has been made possible only with the contribution of the awesome work done by:

- [@albertogeniola]
- [@bytespider]

Have a look at their repositories to better understand how the Meross line of devices is working through MQTT.
I really thank them for the inspiration and the knowledge that made it possible for me to develop this integration

[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[@albertogeniola]: https://github.com/albertogeniola/MerossIot
[@bytespider]: https://github.com/bytespider/Meross
