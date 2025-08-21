import asyncio
import logging
import sys

from songpal import SongpalException
from songpal.discovery import DiscoveredDevice, Discover

import config
from avr import SonyDevice
from config import AvrDevice

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)
_LOG: logging.Logger

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
        _LOG.info("Discovered device %s : %s", discovered_device.name, discovered_device.endpoint)

    try:
        _LOG.debug("Starting discovery")
        _found_devices = []
        await Discover.discover(5, _LOG.level, callback=discovered_devices)
        return _found_devices
    except Exception as ex:  # pylint: disable=broad-exception-caught
        _LOG.error("Failed to start discovery: %s", ex)
        return []

async def main():
    global _LOG
    devices = await sony_avrs()
    host = devices[0].endpoint if len(devices) > 0 else "192.168.1.51"
    try:
        device: AvrDevice = await config.Devices.extract_device_info(host)
    except SongpalException as ex:
        _LOG.error("Cannot connect to %s: %s", host, ex)
        return

    if device is None or device.id is None:
        _LOG.error(
            "Could not get mac address of host %s: required to create a unique device",
            host,
        )

    client: SonyDevice = SonyDevice(device, _LOOP)
    await client.connect()
    await asyncio.sleep(5)
    _LOG.info("Volume : %s (%s - %s)", client.volume_level, client._volume_min, client._volume_max)
    _LOG.debug("END")


if __name__ == "__main__":
    _LOG = logging.getLogger(__name__)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logging.basicConfig(handlers=[ch])
    logging.getLogger("discover").setLevel(logging.DEBUG)
    logging.getLogger("songpal").setLevel(logging.DEBUG)
    logging.getLogger(__name__).setLevel(logging.DEBUG)
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()