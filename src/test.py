import asyncio
import logging
import sys

from songpal.discovery import DiscoveredDevice, Discover

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)

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
    await sony_avrs()
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