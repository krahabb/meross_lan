![GitHub last commit](https://img.shields.io/github/last-commit/krahabb/meross_lan?style=for-the-badge)
[![GitHub](https://img.shields.io/github/license/krahabb/meross_lan?style=for-the-badge)](LICENCE)
[![hacs][hacsbadge]][hacs]


# Meross LAN

This [homeassistant](https://www.home-assistant.io/) integration allows you to control your *Meross* devices all over your LAN without any need for cloud connectivity. It supports communication through your own MQTT broker (or any other configured through the homeassistant mqtt integration) or directly via HTTP.

These are the two main use cases:
- Keep your devices paired with the offical Meross App (and cloud infrastructure) and communicate directly to them via HTTP. This will allow for greater flexibility and less configuration pain since you don't have to setup and configure the MQTT pairing of these devices. The integration will just 'side-communicate' over HTTP to the devices and poll them for status updates. (This is different from https://github.com/albertogeniola/meross-homeassistant since this componenent does not talk to the Meross Cloud service so it doesn't use credentials - except for key retrieval - nor it suffers from throttling or banning)
- Bind your devices to your 'private' MQTT broker so to completely disconnect them from the Meross infrastructure and interact only locally (The procedure for MQTT binding is here: https://github.com/bytespider/Meross/wiki/MQTT or better, you can use the [pairer app](https://play.google.com/store/apps/details?id=com.albertogeniola.merossconf) from @albertogeniola at https://github.com/albertogeniola/meross_pair)

HAVE FUN! 😎

## Installation

### HACS

In your HA frontend go to `HACS -> Integrations`, search for 'Meross LAN' and hit 'Install'
You'll have to restart HA to let it recognize the new integration.

### Manual installation

Download and copy the `custom_components/meross_lan` directory into the `custom_components` folder on your homeassistant installation.

Depending on the type of HA installation you might have to follow specific instructions.

This is working for a standard 'core' installation but should work for any other flavour: remember to set the appropriate ownership and access rights on your copied files so the homeassistant user running your instance is able to read and execute the integration code.

Restart HA to let it play.

## Setup

Once installed and restarted, your Meross devices should be automatically discovered by the 'dhcp' integration and will then pop-up in your integrations panel ready to be configured (the exact timing will depend since the dhcp discovery has different strategies but a simple boot of the device should be sufficient even if not necessary).

> ℹ️ If device(s) are not automatically discovered, try powering them off for 10s and then powering them back on. A notification that new devices have been discovered should appear in `notifications`.

If you are using the 'MQTT way' devices will be automatically discovered when they publish any MQTT message topic.
If you set a non-empty device key when binding your devices to your private broker, you must configure it in the 'MQTT Hub' configuration entry (this will popup too in the discovered integrations). Once the mqtt hub entry is properly set with the correct key, devices should be automatically discovered

You can also manually add your device by adding a new integration entry and providing the host address and device key (repeat this for every device to add).

When configuring a device entry you'll have the option to set:
- host address: this is available when manually adding a device or when a device is discovered via DHCP: provide the ip address or a valid network host name. When you set the ip address, try to ensure it is 'stable' and not changing between re-boots else the integration could 'loose' access to the device. Starting from version 2.7.0 any dynamic ip change should be recognized by meross_lan so you don't have to manually fix this anymore.
- device key: this is used to sign messages according to the official Meross protocol behaviour. This should be prefilled with a known key from other devices if you already configured any before. If you enter a wrong or empty key a menu will ask you if you want to manually retry entering a different key or if you want to recover the key from your Meross account. If your device is still paired to the Meross App, this is the way to recover the device key since it is managed by the Meross App and saved in your cloud profile.

These other options are available once the device is setup the first time. To access them just access the integration configuration UI:
- protocol: the software is able to communicate both over http directly to the device or through an mqtt broker. When you configure an entry by ip address (either manually or dhcp discovered) it usually 'prefers' to talk http for obvious reasons but can nevertheless automatically switch to mqtt if it recognizes it is available (by 'sensing' mqtt messages flowing through). If you set 'Auto' (or leave empty/unconfigured) you'll have this automatic 'failover' switch in both directions (HTTP <-> MQTT) trying to always ensure the best available transport to communicate. If you force it (either HTTP or MQTT) no automatic protocol switching will occur and the integration will only talk that protocol for that configuration entry (some minor exceptions are in place at the moment and some commands are tried over HTTP first anyway).
- polling: sets the polling period (default is 30 sec) for the device. Devices are generally polled to update their status. There are some optimizations so, for example, if the device is connected through MQTT many general status update requests are automatically 'dropped' since the integration can rely on the device 'PUSH' behaviour (this works if you set protocol 'AUTO' too). Some other status info anyway need to be polled (an example is power/energy readings for power metered plugs) even on MQTT and so the polling is in place 'lightly' even on MQTT. If the device is only reachable on HTTP the integration will nevertheless perform a 'full' status update on every polling cycle. Beware some info are polled on an internal (fixed and probably longer) timeout regardless of the configuration parameter you set.
- time zone: you can enter your local time zone from the preset list so your device will be set accordingly. Every device tries to get the actual (UTC) time when booting but, expecially if you unpaired it from the Meross cloud service, its time-zone informations are empty since it doesn't know where it lives. This could give some [issues](https://github.com/krahabb/meross_lan/issues/36) so, in order to fix it, it is better to let them know where they live. The integration is not able at the moment to set the device time so ensure your appliances are able to reach an NTP server (they do so at startup).
- debug tracing: when enabling this option the integration will start to dump every protocol exchange for that device together with relevant logs until timeout is reached. The trace is saved under 'custom_components/meross_lan/traces' (see [wiki](https://github.com/krahabb/meross_lan/wiki/Diagnostics)).
- debug tracing duration: set the duration of the 'debug tracing' feature. This value will be used as a timeout for tracing data collection. See 'debug tracing' option and the [wiki](https://github.com/krahabb/meross_lan/wiki/Diagnostics) for insights.

## Supported hardware

Most of this software has been developed and tested on my owned Meross devices which, over the time, are slowly expanding. I have tried to make it the more optimistic and generalistic as possible based on the work from [@albertogeniola] and [@bytespider] so it should work with most of the hardware out there but I did not test anything other than mines. There are some user reports confirming it works with other devices and the 'official' complete list is here (keep in mind some firmware versions might work while other not: this is the 'hell' of hw & sw):

- Switches
  - [MSS110](https://www.meross.com/Detail/58/Smart%20Wi-Fi%20Plug%20Mini): Smart Wifi plug mini
  - [MSS210](https://www.meross.com/Detail/3/Smart%20Wi-Fi%20Plug): Smart Wifi plug
  - [MSS310](https://www.meross.com/Detail/38/Smart%20Wi-Fi%20Plug%20with%20Energy%20Monitor): power plug with metering capabilties
  - [MSS425](https://www.meross.com/Detail/16/Smart%20Wi-Fi%20Surge%20Protector): Smart WiFi Surge Protector (multiple sockets power strip)
  - [MSS510](https://www.meross.com/Detail/23/Smart%20Wi-Fi%20Single%20Pole%20Switch): Smart WiFi single pole switch
  - [MSS550](https://www.meross.com/Detail/24/Smart%20Wi-Fi%203-Way%20Switch): Smart WiFi 2 way switch
  - [MSS620](https://www.meross.com/Detail/20/Smart%20Wi-Fi%20Indoor-Outdoor%20Plug): Smart WiFi Indoor/Outdoor Plug
  - [MSS710](https://www.meross.com/Detail/21/Smart%20Wi-Fi%20Switch): Smart WiFi DIY switch
- Lights
  - [MSL100](https://www.meross.com/product/4/article/): Smart bulb with dimmable light
  - [MSL120](https://www.meross.com/product/28/article/): Smart RGB bulb with dimmable light
  - [MSL320](https://www.meross.com/Detail/86/Smart%20Wi-Fi%20Light%20Strip): Smart Wifi Light Strip
  - [MSL420](https://www.meross.com/product/22/article): Smart Ambient Light
- Hub
  - [MSH300](https://www.meross.com/Detail/50/Smart%20Wi-Fi%20Hub): Smart WiFi Hub
- Sensors
  - [MS100](https://www.meross.com/Detail/46/Smart%20Temperature%20and%20Humidity%20Sensor): Smart Temperature/Humidity Sensor
- Thermostats
  - [MTS100](https://www.meross.com/Detail/30/Smart%20Thermostat%20Valve): Smart Thermostat Valve
  - [MTS200](https://www.meross.com/Detail/116/Smart%20Wi-Fi%20Thermostat): Smart Wifi Thermostat
- Covers
  - [MRS100](https://www.meross.com/product/91/article/): Smart WiFi Roller Shutter
  - [MSG100](https://www.meross.com/product/29/article/): Smart WiFi Garage Door Opener
- Humidifiers
  - [MSXH0](https://www.meross.com/Detail/47/Smart%20Wi-Fi%20Humidifier) [experimental]: Smart WiFi Humidifier
  - [MOD100](https://www.meross.com/Detail/93/Smart%20Wi-Fi%20Essential%20Oil%20Diffuser) [experimental]: Smart WiFi Essential Oil Diffuser
- Smart Cherub Baby Machine
  - [HP110A](https://www.meross.com/product/53/article/) [experimental]: Smart Cherub Baby Machine

## Features

The component exposes the basic functionality of the underlying device (toggle on/off, dimm, report consumption through sensors) without any other effort. It should be able to detect if the device goes offline suddenly by using a periodic heartbeat.
It also features an automatic protocol switching capability so, if you have your MQTT setup and your broker dies or whatever, the integration will try to fallback to HTTP communication and keep the device available returning automatically to MQTT mode as soon as the MQTT infrastructure returns online. The same works for HTTP mode: when the device is not reachable it will try to use MQTT (provided it is available!). This feature is enabled by default for every new configuration entry and you can control it by setting the 'Protocol' field in the configration panel of the integration: setting 'AUTO' (or empty) will do the automatic switch. Setting any fixed protocol 'MQTT' or 'HTTP' will force the use of that option (useful if you're in trouble and want to isolate or investigate inconsistent behaviours). I'd say: leave it empty or 'AUTO' it works good in my tests.

If you have the MSH300 Hub working with this integration, every new subdevice (thermostat or sensor) can be automatically discovered once the subdevice is paired with the hub. When the hub is configured in this integration you don't need to switch back and forth to/from the Meross app in order to 'bind' new devices: just pair the thermostat or sensor to the hub by using the subdevice pairing procedure (fast double press on the hub).

DND mode (status/presence light) is supported through a light entity. This entity is marked as a 'configuration entity' in HA terms and is by default visible in the device page in HA UI. If you want to access it in your lovelace cards you have to manually add it. When the device is in 'do-not-disturb' mode the light will be switched off so the device doesn't pollute your home environment!

Wifi signal strength is available for every meross wifi device and could be accessed on the device page. It is refreshed every 5 minutes.

In general, many device configuration options available in Meross app are not supported in meross_lan though some are. As an example, the thermostats preset temperatures (for heat, cool, eco/away) are accessible in HA/meross_lan exactly as if you were to set them manually on the device or via the app. These, and any other supported configuration options, are available as configuration entities (so they're not added to the default lovelace dashboard) and you can access them by going to the relevant device page in HA Configuration -> Devices

## Service

There is a service called `meross_lan.request` exposed to simplify communication with the device and play with it a bit. It basically requires the needed informations to setup a command request and send it over MQTT or HTTP without the hassle of signatures and timestamps computations. You can check it in the 'Developer Tools' of the HA instance, everything should be enough self-explanatory there.
Since version 3.0.2 the service allows you to publish the device response as a persistent_notification message in HA so you don't have to dig into logs in order to see the reply. The notification is optional and you have to add the `notifyresponse` key to the service call with a value of `true`

## Troubleshooting

Check the [wiki](https://github.com/krahabb/meross_lan/wiki/Diagnostics) for detailed informations on how to gather diagnostics data for meross_lan devices

## References

This integration has been made possible only with the contribution of the awesome work done by:

- [@albertogeniola]
- [@bytespider]

Have a look at their repositories to better understand how the Meross line of devices is working through MQTT.
I really thank them for the inspiration and the knowledge that made it possible for me to develop this integration.

Special mention also for:
- [@nao-pon](https://github.com/nao-pon)
- [@wsw70](https://github.com/wsw70)
- [@gelokatil](https://github.com/gelokatil)
- [@scannifn](https://github.com/scannifn)
- [@almico](https://github.com/almico)
- [@Gronda74](https://github.com/Gronda74)
- [@GeorgeCaliment](https://github.com/GeorgeCaliment)

who greatly helped me fixing issues or developing nice enhancements to the component

[hacs]: https://github.com/custom-components/hacs
[hacsbadge]: https://img.shields.io/badge/HACS-Default-orange.svg?style=for-the-badge
[@albertogeniola]: https://github.com/albertogeniola/MerossIot
[@bytespider]: https://github.com/bytespider/Meross
