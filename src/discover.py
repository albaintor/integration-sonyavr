"""
Sony AVR device discovery with SSDP.

:copyright: (c) 2023 by Albaintor
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging

from songpal.discovery import Discover, DiscoveredDevice

_LOG = logging.getLogger(__name__)

TIMEOUT = 5
_found_devices: list[DiscoveredDevice] = []


async def sony_avrs() -> list[DiscoveredDevice]:
    """
    Discover Sony AVRs on the network with SSDP.

    Returns a list of dictionaries which includes all discovered Sony AVR
    devices with keys "host", "modelName", "friendlyName", "presentationURL".
    By default, SSDP broadcasts are sent once with a 2 seconds timeout.

    :return: array of device information objects.
    """
    global _found_devices

    async def discovered_devices(discovered_device: DiscoveredDevice):
        _found_devices.append(discovered_device)

    try:
        _LOG.debug("Starting discovery")
        _found_devices = []
        await Discover.discover(TIMEOUT, _LOG.level, callback=discovered_devices)
        return _found_devices
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _LOG.error("Failed to start discovery: %s", ex)
        return []
