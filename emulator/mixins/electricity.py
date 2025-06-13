""""""

from datetime import datetime, timezone
from random import randint
from time import gmtime
from typing import TYPE_CHECKING

from custom_components.meross_lan.merossclient.protocol import (
    const as mc,
    namespaces as mn,
)

if TYPE_CHECKING:
    from .. import MerossEmulator, MerossEmulatorDescriptor


class ElectricityMixin(MerossEmulator if TYPE_CHECKING else object):
    # used to 'fix' and control the power level in tests
    # if None (default) it will generate random values
    _power_set: int | None = None
    # this is 'shared' with ConsumptionXMixin to control tests output
    power: int

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)
        self.payload_electricity = descriptor.namespaces[
            mn.Appliance_Control_Electricity.name
        ]
        self.electricity = self.payload_electricity[mc.KEY_ELECTRICITY]
        self.voltage_average: int = self.electricity[mc.KEY_VOLTAGE] or 2280
        self.power = self.electricity[mc.KEY_POWER]
        self._power_set = None

    def set_power(self, power: int | None):
        self._power_set = power
        if power is not None:
            self.power = self.electricity[mc.KEY_POWER] = power

    def _GET_Appliance_Control_Electricity(self, header, payload):
        """
        {
            "electricity": {
                "channel":0,
                "current":34,
                "voltage":2274,
                "power":1015,
                "config":{"voltageRatio":188,"electricityRatio":100}
            }
        }
        """
        if self._power_set is None:
            if randint(0, 5) == 0:
                # make a big power step
                power = self.power + randint(-1000000, 1000000)
            else:
                # make some noise
                power = self.power + randint(-1000, 1000)
            if power > 3600000:
                power = 3600000
            elif power < 0:
                power = 0
        else:
            power = self._power_set

        p_electricity = self.electricity
        p_electricity[mc.KEY_POWER] = self.power = power
        p_electricity[mc.KEY_VOLTAGE] = self.voltage_average + randint(-20, 20)
        p_electricity[mc.KEY_CURRENT] = int(10 * power / p_electricity[mc.KEY_VOLTAGE])
        return mc.METHOD_GETACK, self.payload_electricity


class ElectricityXMixin(MerossEmulator if TYPE_CHECKING else object):

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)
        self.payload_electricityx = descriptor.namespaces.setdefault(
            mn.Appliance_Control_ElectricityX.name,
            {
                mc.KEY_ELECTRICITY: [
                    {
                        "channel": 1,
                        "current": 0,
                        "voltage": 233680,
                        "power": 0,
                        "mConsume": 1967,
                        "factor": 0,
                    },
                    {
                        "channel": 2,
                        "current": 574,
                        "voltage": 233184,
                        "power": 115185,
                        "mConsume": 4881,
                        "factor": 0.8602570295333862,
                    },
                    {
                        "channel": 3,
                        "current": 0,
                        "voltage": 232021,
                        "power": 0,
                        "mConsume": 59,
                        "factor": 0,
                    },
                    {
                        "channel": 4,
                        "current": 311,
                        "voltage": 233748,
                        "power": 324,
                        "mConsume": 0,
                        "factor": 0.004454255104064941,
                    },
                    {
                        "channel": 5,
                        "current": 0,
                        "voltage": 233313,
                        "power": 0,
                        "mConsume": 0,
                        "factor": 0,
                    },
                    {
                        "channel": 6,
                        "current": 339,
                        "voltage": 232127,
                        "power": -10,
                        "mConsume": 0,
                        "factor": -0.0001285076141357422,
                    },
                ]
            },
        )
        self.electricityx = self.payload_electricityx[mc.KEY_ELECTRICITY]

    def _GET_Appliance_Control_ElectricityX(self, header, payload):
        return mc.METHOD_GETACK, self.payload_electricityx


class ConsumptionXMixin(MerossEmulator if TYPE_CHECKING else object):
    # this is a static default but we're likely using
    # the current 'power' state managed by the ElectricityMixin
    power = 0.0  # in mW

    BUG_RESET = True

    def __init__(self, descriptor: "MerossEmulatorDescriptor", key):
        super().__init__(descriptor, key)
        self.payload_consumptionx = descriptor.namespaces[
            mn.Appliance_Control_ConsumptionX.name
        ]
        p_consumptionx: list = self.payload_consumptionx[mc.KEY_CONSUMPTIONX]
        if not p_consumptionx:
            p_consumptionx.append(
                {
                    mc.KEY_DATE: "1970-01-01",
                    mc.KEY_TIME: 0,
                    mc.KEY_VALUE: 1,
                }
            )
        else:

            def _get_timestamp(consumptionx_item):
                return consumptionx_item[mc.KEY_TIME]

            p_consumptionx = sorted(p_consumptionx, key=_get_timestamp)
            self.payload_consumptionx[mc.KEY_CONSUMPTIONX] = p_consumptionx

        self.consumptionx = p_consumptionx
        self._epoch_prev = 0
        self._power_prev = None
        self._energy_fraction = 0.0  # in Wh
        # REMOVE
        # "Asia/Bangkok" GMT + 7
        # "Asia/Baku" GMT + 4
        self.set_timezone("Asia/Baku")

    def _mqttc_subscribe(self, *args):
        super()._mqttc_subscribe(*args)
        # kind of Bind message..we're just interested in validating
        # the server code in meross_lan (it doesn't really check this
        # payload)
        self.mqtt_publish_push(
            mn.Appliance_Control_ConsumptionConfig.name,
            {
                mn.Appliance_Control_ConsumptionConfig.key: {
                    "voltageRatio": 188,
                    "electricityRatio": 102,
                    "maxElectricityCurrent": 11000,
                    "powerRatio": 0,
                }
            },
        )

    def _PUSH_Appliance_Control_ConsumptionConfig(self, header, payload):
        return None, None

    def _GET_Appliance_Control_ConsumptionX(self, header, payload):
        """
        {
            "consumptionx": [
                {"date":"2023-03-01","time":1677711486,"value":52},
                {"date":"2023-03-02","time":1677797884,"value":53},
                {"date":"2023-03-03","time":1677884282,"value":51},
                ...
            ]
        }
        """
        # energy will be reset every time we update our consumptionx array
        if self._power_prev is not None:
            self._energy_fraction += (
                (self.power + self._power_prev)
                * (self.epoch - self._epoch_prev)
                / 7200000
            )
        self._epoch_prev = self.epoch
        self._power_prev = self.power

        if self._energy_fraction < 1.0:
            return mc.METHOD_GETACK, self.payload_consumptionx

        energy = int(self._energy_fraction)
        self._energy_fraction -= energy

        y, m, d, hh, mm, ss, weekday, jday, dst = gmtime(self.epoch)
        devtime = datetime(y, m, d, hh, mm, min(ss, 59), 0, timezone.utc)
        if tzinfo := self.tzinfo:
            devtime = devtime.astimezone(tzinfo)

        date_value = "{:04d}-{:02d}-{:02d}".format(
            devtime.year, devtime.month, devtime.day
        )

        p_consumptionx = self.consumptionx
        consumptionx_last = p_consumptionx[-1]
        if consumptionx_last[mc.KEY_DATE] != date_value:
            if len(p_consumptionx) >= 30:
                p_consumptionx.pop(0)
            p_consumptionx.append(
                {
                    mc.KEY_DATE: date_value,
                    mc.KEY_TIME: self.epoch,
                    mc.KEY_VALUE: (
                        energy + consumptionx_last[mc.KEY_VALUE]
                        if self.BUG_RESET
                        else 0
                    ),
                }
            )

        else:
            consumptionx_last[mc.KEY_TIME] = self.epoch
            consumptionx_last[mc.KEY_VALUE] += energy

        return mc.METHOD_GETACK, self.payload_consumptionx
