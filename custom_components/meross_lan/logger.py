import logging
from time import time

LOGGER = logging.getLogger(__name__[:-7]) #get base custom_component name for logging


_trap_msg = None
_trap_args = None
_trap_time = 0
_trap_level = 0

def LOGGER_trap(level, msg, *args):
    """
    avoid repeating the same last log message until something changes or timeout expires
    used mainly when discovering new devices
    """
    global _trap_msg
    global _trap_args
    global _trap_time
    global _trap_level

    tm = time()
    if (_trap_level == level) \
        and (_trap_msg == msg) \
        and (_trap_args == args) \
        and ((tm - _trap_time) < 300): # 5 minutes timeout
        return

    LOGGER.log(level, msg, *args)
    _trap_msg = msg
    _trap_args = args
    _trap_time = tm
    _trap_level = level
