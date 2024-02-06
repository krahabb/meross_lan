""""""

from __future__ import annotations

import asyncio
import typing

from custom_components.meross_lan.merossclient import const as mc, get_element_by_key

if typing.TYPE_CHECKING:
    from .. import MerossEmulator


class RollerShutterMixin(MerossEmulator if typing.TYPE_CHECKING else object):
    pass