"""
Configuration handling of the integration driver.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import dataclasses
from urllib.parse import urlparse

from songpal import Device

import discover
import json
import logging
import os
from asyncio import Lock
from dataclasses import dataclass
from typing import Iterator, Callable

from ucapi import EntityTypes

from const import DEFAULT_PORT

_LOG = logging.getLogger(__name__)

_CFG_FILENAME = "config.json"


def create_entity_id(avr_id: str, entity_type: EntityTypes) -> str:
    """Create a unique entity identifier for the given receiver and entity type."""
    return f"{entity_type.value}.{avr_id}"


def avr_from_entity_id(entity_id: str) -> str | None:
    """
    Return the avr_id prefix of an entity_id.

    The prefix is the part before the first dot in the name and refers to the AVR device identifier.

    :param entity_id: the entity identifier
    :return: the device prefix, or None if entity_id doesn't contain a dot
    """
    return entity_id.split(".", 1)[1]


@dataclass
class AvrDevice:
    """Sony device configuration."""

    # pylint: disable = W0622
    id: str
    name: str
    address: str
    always_on: bool
    volume_step: float
    mac_address_wired: str | None
    mac_address_wifi: str | None


class _EnhancedJSONEncoder(json.JSONEncoder):
    """Python dataclass json encoder."""

    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)


class Devices:
    """Integration driver configuration class. Manages all configured Sony devices."""

    def __init__(self, data_path: str,
                 add_handler: Callable[[AvrDevice], None],
                 remove_handler: Callable[[AvrDevice | None], None],
                 update_handler: Callable[[AvrDevice], None]):
        """
        Create a configuration instance for the given configuration path.

        :param data_path: configuration path for the configuration file and client device certificates.
        """
        self._data_path: str = data_path
        self._cfg_file_path: str = os.path.join(data_path, _CFG_FILENAME)
        self._config: list[AvrDevice] = []
        self._add_handler = add_handler
        self._remove_handler = remove_handler
        self._update_handler = update_handler
        self.load()
        self._config_lock = Lock()

    @property
    def data_path(self) -> str:
        """Return the configuration path."""
        return self._data_path

    def all(self) -> Iterator[AvrDevice]:
        """Get an iterator for all devicall()e configurations."""
        return iter(self._config)

    def empty(self) -> bool:
        """Return true if no devices configured."""
        return len(self._config) == 0

    def contains(self, avr_id: str) -> bool:
        """Check if there's a device with the given device identifier."""
        for item in self._config:
            if item.id == avr_id:
                return True
        return False

    def add_or_update(self, atv: AvrDevice) -> None:
        """Add a new configured device."""
        if self.contains(atv.id):
            _LOG.debug("Existing config %s, updating it %s", atv.id, atv)
            self.update(atv)
            if self._update_handler is not None:
                self._update_handler(atv)
        else:
            _LOG.debug("Adding new config %s", atv)
            self._config.append(atv)
            self.store()
        if self._add_handler is not None:
            self._add_handler(atv)

    def get(self, avr_id: str) -> AvrDevice | None:
        """Get device configuration for given identifier."""
        for item in self._config:
            if item.id == avr_id:
                # return a copy
                return dataclasses.replace(item)
        return None

    def update(self, device: AvrDevice) -> bool:
        """Update a configured Sony device and persist configuration."""
        for item in self._config:
            if item.id == device.id:
                item.address = device.address
                item.name = device.name
                item.always_on = device.always_on
                item.volume_step = device.volume_step
                item.mac_address_wired = device.mac_address_wired
                item.mac_address_wired = device.mac_address_wired
                return self.store()
        return False

    def remove(self, avr_id: str) -> bool:
        """Remove the given device configuration."""
        device = self.get(avr_id)
        if device is None:
            return False
        try:
            self._config.remove(device)
            if self._remove_handler is not None:
                self._remove_handler(device)
            return True
        except ValueError:
            pass
        return False

    def clear(self) -> None:
        """Remove the configuration file."""
        self._config = []

        if os.path.exists(self._cfg_file_path):
            os.remove(self._cfg_file_path)

        if self._remove_handler is not None:
            self._remove_handler(None)

    def store(self) -> bool:
        """
        Store the configuration file.

        :return: True if the configuration could be saved.
        """
        try:
            with open(self._cfg_file_path, "w+", encoding="utf-8") as f:
                json.dump(self._config, f, ensure_ascii=False, cls=_EnhancedJSONEncoder)
            return True
        except OSError:
            _LOG.error("Cannot write the config file")

        return False

    def load(self) -> bool:
        """
        Load the config into the config global variable.

        :return: True if the configuration could be loaded.
        """
        try:
            with open(self._cfg_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                # not using AtvDevice(**item) to be able to migrate old configuration files with missing attributes
                device_instance = AvrDevice(
                    item.get("id"),
                    item.get("name"),
                    item.get("address"),
                    item.get("always_on", False),
                    item.get("volume_step", 2.0),
                    item.get("mac_address_wired", None),
                    item.get("mac_address_wifi", None)
                )
                self._config.append(device_instance)
            return True
        except OSError:
            _LOG.error("Cannot open the config file")
        except ValueError:
            _LOG.error("Empty or invalid config file")

        return False

    @staticmethod
    async def extract_device_info(host: str) -> AvrDevice:
        """Extract device information from host."""
        if not host.startswith("http://"):
            host = f"http://{host}"

        result = urlparse(host)
        path = result.path
        port = result.port
        if not path:
            path = "/sony"
        if not port:
            port = DEFAULT_PORT
        host = f"{result.scheme}://{result.hostname}:{port}{path}"

        # simple connection check
        device = Device(host)
        await device.get_supported_methods()
        interface_info = await device.get_interface_information()
        system_info = await device.get_system_info()

        assert device
        assert system_info

        unique_id = system_info.serialNumber
        if unique_id is None:
            unique_id = system_info.macAddr
        if unique_id is None:
            unique_id = system_info.wirelessMacAddr

        return AvrDevice(
            id=unique_id,
            name=interface_info.modelName,
            address=host,
            always_on=False,
            volume_step=2,
            mac_address_wired=system_info.macAddr,
            mac_address_wifi=system_info.wirelessMacAddr
        )

    async def handle_address_change(self):
        """Check for address change and update configuration"""
        if devices.empty():
            return
        if self._config_lock.locked():
            _LOG.debug("Check device change already in progress")
            return False

        # Only one instance of devices change
        await self._config_lock.acquire()
        _discovered_devices = await discover.sony_avrs()
        _discovered_configs: [AvrDevice] = []
        _devices_changed: [AvrDevice] = []

        for _discovered_device in _discovered_devices:
            try:
                _discovered_configs.append(await Devices.extract_device_info(_discovered_device.endpoint))
            except Exception:
                pass

        for device_config in devices.all():
            found = False
            for device in _discovered_configs:
                if device_config.mac_address_wifi and (device_config.mac_address_wifi == device.mac_address_wired
                                                       or device_config.mac_address_wifi == device.mac_address_wifi):
                    found = True
                elif device_config.mac_address_wired and (device_config.mac_address_wired == device.mac_address_wired
                                                          or device_config.mac_address_wired == device.mac_address_wifi):
                    found = True

                if found:
                    if device_config.address == device.address:
                        _LOG.debug("Found device %s with unchanged address %s", device_config.name,
                                   device_config.address)
                    elif device.address:
                        _LOG.debug("Found device %s with new address %s -> %s", device_config.name,
                                   device_config.address, device.address)
                        device_config.address = device.address
                        _configuration_changed = True
                    break
            if not found:
                _LOG.debug("Device %s (%s) not found, probably off", device_config.name, device_config.address)

        if len(_devices_changed) > 0:
            self.store()
            _LOG.debug("Configuration updated")
            if self._update_handler is not None:
                for device in _devices_changed:
                    self._update_handler(device)

        self._config_lock.release()


devices: Devices | None = None
