"""
Select entity functions.

:copyright: (c) 2026 by Albaintor
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from ucapi import EntityTypes, Select, StatusCodes
from ucapi.api_definitions import CommandHandler
from ucapi.select import Attributes, Commands, States

import avr
from config import DeviceInstance, SonyEntity, create_entity_id
from const import SonySelects

_LOG = logging.getLogger(__name__)


# pylint: disable=W1405,R0801
class SonySelect(SonyEntity, Select):
    """Representation of a Sony AVR select entity."""

    ENTITY_NAME = "select"
    SELECT_NAME: SonySelects

    # pylint: disable=R0917
    def __init__(
        self,
        entity_id: str,
        name: str | dict[str, str],
        device_config: DeviceInstance,
        device: avr.SonyDevice,
        select_handler: CommandHandler,
    ):
        """Initialize the class."""
        # pylint: disable = R0801
        attributes = dict[Any, Any]()
        self._device_config = device_config
        self._device: avr.SonyDevice = device
        self._state: States = States.ON
        self._select_handler: CommandHandler = select_handler
        super().__init__(
            identifier=entity_id,
            name=name,
            attributes=attributes,
        )

    @property
    def deviceid(self) -> str:
        """Return device identifier."""
        return self._device_config.id

    @property
    def current_option(self) -> str:
        """Return select value."""
        raise NotImplementedError()

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        raise NotImplementedError()

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated selector value from full update if provided or sensor value if no udpate is provided."""
        _LOG.debug("[%s] Update selector %s", self._device_config.address, update)
        if update:
            if self.SELECT_NAME in update:
                return update[self.SELECT_NAME]
            return None
        return {
            Attributes.CURRENT_OPTION: self.current_option,
            Attributes.OPTIONS: self.select_options,
            Attributes.STATE: States.ON,
        }

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None, *, websocket: Any) -> StatusCodes:
        """Process selector command."""
        # pylint: disable=R0911
        if cmd_id == Commands.SELECT_OPTION and params:
            option = params.get("option", None)
            return await self._select_handler(option)
        options = self.select_options
        if cmd_id == Commands.SELECT_FIRST and len(options) > 0:
            return await self._select_handler(options[0])
        if cmd_id == Commands.SELECT_LAST and len(options) > 0:
            return await self._select_handler(options[len(options) - 1])
        if cmd_id == Commands.SELECT_NEXT and len(options) > 0:
            cycle = params.get("cycle", False)
            try:
                index = options.index(self.current_option) + 1
                if not cycle and index >= len(options):
                    return StatusCodes.OK
                if index >= len(options):
                    index = 0
                return await self._select_handler(options[index])
            except ValueError as ex:
                _LOG.warning(
                    "[%s] Invalid option %s in list %s %s",
                    self._device_config.address,
                    self.current_option,
                    options,
                    ex,
                )
                return StatusCodes.BAD_REQUEST
        if cmd_id == Commands.SELECT_PREVIOUS and len(options) > 0:
            cycle = params.get("cycle", False)
            try:
                index = options.index(self.current_option) - 1
                if not cycle and index < 0:
                    return StatusCodes.OK
                if index < 0:
                    index = len(options) - 1
                return await self._select_handler(options[index])
            except ValueError as ex:
                _LOG.warning(
                    "[%s] Invalid option %s in list %s %s",
                    self._device_config.address,
                    self.current_option,
                    options,
                    ex,
                )
                return StatusCodes.BAD_REQUEST
        return StatusCodes.BAD_REQUEST


class SonyInputSourceSelect(SonySelect):
    """Input source selector entity."""

    ENTITY_NAME = "input_source"
    SELECT_NAME = SonySelects.SELECT_INPUT_SOURCE

    def __init__(self, device_config: DeviceInstance, device: avr.SonyDevice):
        """Initialize the class."""
        # pylint: disable=W1405,R0801
        entity_id = f"{create_entity_id(device_config.id, EntityTypes.SELECT)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{device_config.get_device_part()}Input source",
                "fr": f"{device_config.get_device_part()}Source",
            },
            device_config,
            device,
            device.select_source,
        )

    @property
    def current_option(self) -> str:
        """Return selector value."""
        return self._device.source if self._device.source else ""

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        return self._device.source_list


class SonySoundModeSelect(SonySelect):
    """Sound mode selector entity."""

    ENTITY_NAME = "sound_mode"
    SELECT_NAME = SonySelects.SELECT_INPUT_SOURCE

    def __init__(self, device_config: DeviceInstance, device: avr.SonyDevice):
        """Initialize the class."""
        # pylint: disable=W1405,R0801
        entity_id = f"{create_entity_id(device_config.id, EntityTypes.SELECT)}.{self.ENTITY_NAME}"
        super().__init__(
            entity_id,
            {
                "en": f"{device_config.get_device_part()}Sound mode",
                "fr": f"{device_config.get_device_part()}Mode sonore",
            },
            device_config,
            device,
            device.select_sound_mode,
        )

    @property
    def current_option(self) -> str:
        """Return selector value."""
        return self._device.sound_mode

    @property
    def select_options(self) -> list[str]:
        """Return selection list."""
        return self._device.sound_mode_list
