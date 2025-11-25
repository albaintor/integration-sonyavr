# pylint: skip-file
# flake8: noqa
import asyncio
import logging
import sys
from typing import Any

from rich import print_json
from songpal import SongpalException
from songpal.discovery import Discover, DiscoveredDevice

import config
from avr import Events, SonyDevice
from config import DeviceInstance

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)
_LOG: logging.Logger


async def on_device_update(device_id: str, update: dict[str, Any] | None) -> None:
    print_json(data=update)


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
    # Manuel mode
    device = DeviceInstance(
        id="5501824",
        name="TA-AN1000",
        address="http://192.168.1.51:10000/sony",
        always_on=False,
        volume_step=2,
        mac_address_wired="f8:4e:17:1f:33:2b",
        mac_address_wifi="04:7b:cb:ec:36:bc",
    )
    # Automatic mode
    # host = "192.168.1.51"
    # try:
    #     device: DeviceInstance = await config.Devices.extract_device_info(host)
    #     _LOG.debug("Device info : %s", device)
    # except SongpalException as ex:
    #     _LOG.error("Cannot connect to %s: %s", host, ex)
    #     return
    client = SonyDevice(device=device)
    client.events.on(Events.UPDATE, on_device_update)
    await client.connect()
    await asyncio.sleep(1)
    await client.power_on()
    _LOG.debug(await client._receiver.get_sound_settings("hdmiOutput"))
    await asyncio.sleep(150)


async def main_direct():
    global _LOG
    devices = await sony_avrs()
    host = devices[0].endpoint if len(devices) > 0 else "192.168.1.51"
    try:
        device: DeviceInstance = await config.Devices.extract_device_info(host)
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
    await asyncio.sleep(100)


if __name__ == "__main__":
    _LOG = logging.getLogger(__name__)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logging.basicConfig(handlers=[ch])
    logging.getLogger("avr").setLevel(logging.DEBUG)
    logging.getLogger("discover").setLevel(logging.DEBUG)
    # logging.getLogger("songpal").setLevel(logging.DEBUG)
    logging.getLogger(__name__).setLevel(logging.DEBUG)
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
