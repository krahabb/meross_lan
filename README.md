![GitHub last commit](https://img.shields.io/github/last-commit/krahabb/meross_lan?style=for-the-badge)
[![GitHub](https://img.shields.io/github/license/krahabb/meross_lan?style=for-the-badge)](LICENCE)
[![hacs][hacsbadge]][hacs]


# Meross LAN

This [homeassistant](https://www.home-assistant.io/) integration allows you to control your *Meross* devices all over your LAN without any need for cloud connectivity. It supports communication through your own MQTT broker (or any other configured through the homeassistant mqtt integration) or directly via HTTP.

These are the two main use cases:
- Keep your devices paired with the offical Meross App (and cloud infrastructure) and communicate directly to them via HTTP. This will allow for greater flexibility and less configuration pain since you don't have to setup and configure the MQTT pairing of these devices. The integration will just 'side-communicate' over HTTP to the devices and poll them for status updates. (This is different from https://github.com/albertogeniola/meross-homeassistant since my componenent does not talk to the Meross Cloud service so it doesn't use credentials or any)
- Bind your devices to your 'private' MQTT broker so to completely disconnect them from the Meross infrastructure and interact only locally (The procedure for MQTT binding is here: https://github.com/bytespider/Meross/wiki/MQTT or better, you can use the pairer app from @albertogeniola at https://github.com/albertogeniola/meross_pair )

HAVE FUN! 😎

## Installation

### HACS

In your HA frontend go to `HACS -> Integrations`, search for 'Meross LAN' and hit 'Install'
You'll have to restart HA to let it recognize the new integration

### Manual installation

Download and copy the `custom_components/meross_lan` directory into the `custom_components` folder on your homeassistant installation.

Depending on the type of HA installation you might have to follow specific instructions.

This is working for a standard 'core' installation but should work for any other flavour: remember to set the appropriate ownership and access rights on your copied files so the homeassistant user running your instance is able to read and execute the integration code.

Restart HA to let it play

## Setup

Once installed and restarted your Meross devices should be automatically discovered by the 'dhcp' integration and will then pop-up in your integrations panel ready to be configured (the exact timing will depend since the dhcp discovery has different strategies but a simple boot of the device should be sufficient even if not necessary)

If you are using the 'MQTT way' you can help the discovery process by adding the 'MQTT Hub' feature of this integration (This was needed in the previous versions while you should be able to skip this step if the dhcp discovery works fine). If you need, just go to your homeassistant `Configuration -> Integrations` and add the Meross LAN by looking through the list of available ones. Here you can configure the device key used to sign the messages exchanged: this need to be the same key used when re-binding your hardware else the integration will not be able to discover new devices (dhcp discovery should instead work anyway: the key will be asked and set when configuring every single appliance)

You can also manually add your device by adding a new integration entry and providing the host address.

When configuring a device entry you'll have the option to set:
- host address: this is available when manually adding a device or when a device is discovered via DHCP: provide the ip address or a valid network host name. When you set the ip address, ensure it is 'stable' and not changing between re-boots else the integration will 'loose' access to the device
- device key: this is used to sign messages according to the official Meross protocol behaviour. Provide the same key you used to re-configure your appliance or, in case you're side-communicating and/or don't know the key leave it empty: this way the HTTP stack will be instructed to 'hack' the protocol by using a simple trick

## Supported hardware

Most of this software has been developed and tested on my owned Meross devices which, over the time, are slowly expanding. I have tried to make it the more optimistic and generalistic as possible based on the work from [@albertogeniola] and [@bytespider] so it should work with most of the hardware out there but I did not test anything other than mines. There are some user reports confirming it works with other devices and the 'official' complete list is here (keep in mind some firmware versions might work while other not: this is the 'hell' of hw & sw):

- Switches
  - [MSS110](https://www.meross.com/product/2/article/): Smart Wifi plug mini
  - [MSS210](https://www.meross.com/Detail/3/Smart%20Wi-Fi%20Plug): Smart Wifi plug
  - [MSS310](https://www.meross.com/product/38/article/): power plug with metering capabilties
  - [MSS425](https://www.meross.com/product/16/article/): Smart WiFi Surge Protector (multiple sockets power strip)
  - [MSS510](https://www.meross.com/product/23/article/): Smart WiFi single pole switch
  - [MSS710](https://www.meross.com/product/21/article/): Smart WiFi DIY switch
- Lights
  - [MSL100](https://www.meross.com/product/4/article/): Smart bulb with dimmable light
  - [MSL120](https://www.meross.com/product/28/article/): Smart RGB bulb with dimmable light
- Hub
  - [MSH300](https://www.meross.com/Detail/50/Smart%20Wi-Fi%20Hub): Smart WiFi Hub
- Sensors
  - [MS100](https://www.meross.com/Detail/46/Smart%20Temperature%20and%20Humidity%20Sensor): Smart Temperature/Humidity Sensor
- Thermostats
  - [MTS100](https://www.meross.com/Detail/30/Smart%20Thermostat%20Valve): Smart Thermostat Valve
- Covers
  - Support for garage door opener and roller shutter is implemented by guess-work so I'm not expecting flawless behaviour but...could work
  - [MSG100](https://www.meross.com/product/29/article/): Smart Wi-Fi Garage Door Opener

## Features

The component exposes the basic functionality of the underlying device (toggle on/off, dimm, report consumption through sensors) without any other effort, It should be able to detect if the device goes offline suddenly by using a periodic heartbeat.
It also features an automatic protocol switching capability so, if you have your MQTT setup and your broker dies or whatever, the integration will try to fallback to HTTP communication and keep the device available returning automatically to MQTT mode as soon as the MQTT infrastructure returns online. The same works for HTTP mode: when the device is not reachable it will try to use MQTT (provided it is available!). This feature is enabled by default for every new configuration entry and you can control it by setting the 'Protocol' field in the configration panel of the integration: setting 'AUTO' (or empty) will do the automatic switch. Setting any fixed protocol 'MQTT' or 'HTTP' will force the use of that option (useful if you're in trouble and want to isolate or investigate inconsistent behaviours). I'd say: leave it empty or 'AUTO' it works good in my tests.

If you have the MSH300 Hub working with this integration, every new subdevice (thermostat or sensor) can be automatically discovered once the subdevice is paired with the hub. When the hub is configured in this integration you don't need to switch back and forth to/from the Meross app in order to 'bind' new devices: just pair the thermostat or sensor to the hub by using the subdevice pairing procedure (fast double press on the hub)

I'm sorry to not be able to write a complete wiki at the moment in order to better explain some procedures or share my knwoledge about the devices but time is constrained and writing knwoledge bases is always consuming (and sligthly boring I admit). I'm still working on some features and I've put a big effort trying to ensure a frictionless working of this software so I hope you can make use of it without deeper explanations. Something will come, slowly, but if you have any urgent issue or question I will be happy to help (and maybe this will speed up the documentation :)

## Service

There is a service (since version 0.0.4) exposed to simplify communication with the device and play with it a bit. It basically requires the needed informations to setup a command request and send it over MQTT or HTTP without the hassle of signatures and timestamps computations. You can check it in the 'Developer Tools' of the HA instance, everything should be enough self-explanatory there.
I find it a bit frustrating that the HA service infrastructure does not allow to return anything from a service invocation so, the eventual reply from the device will get 'lost' in the mqtt flow. I've personally played a bit with the MQTT integration configuration pane to listen and see the mqtt responses from my devices but it's somewhat a pain unless you have a big screen to play with (or multiple monitors for the matter). Nevertheless you can use the service wherever you like to maybe invoke features at the device level or dig into it's configuration
*WARNING*: the service name has changed from 'mqtt_publish' to 'request' to accomodate the more general protocol support

## Troubleshooting

In order to help troubleshoot issues there is a tracing feature (available since 2.0.2) which dumps the protocol exchange and other debug messages to a text (TAB separated) file without the need and the mess of going through the HA (debug) log. This trace is available 'per device' and you can activate it from the integration configuration UI. Once activated it will start recording for 10 minutes (or maximum 64Kb whichever comes first) and then will stop automatically. If needed you can also stop it manually at any time while in progress by just entering the configuration UI and deselecting the checkbox. The trace(s) will be saved under 'custom_components/meross_lan/traces'. The trace feature takes care of obfuscating some 'sensitive' (like mac(s), Ip(s), and userId(s)) data fields extracted by the protocol. I've taken care of hiding those informations I guess would be nice to, but if you're concerned and find something 'leaking' from my masking please let me know so I can eventually proceed to mask those other infos (For example I didn't take care of some WiFi related message payloads which could carry very sensitive info since my code is not using them in any way at the moment)

## References

This integration has been made possible only with the contribution of the awesome work done by:

- [@albertogeniola]
- [@bytespider]

Have a look at their repositories to better understand how the Meross line of devices is working through MQTT.
I really thank them for the inspiration and the knowledge that made it possible for me to develop this integration

[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[@albertogeniola]: https://github.com/albertogeniola/MerossIot
[@bytespider]: https://github.com/bytespider/Meross
