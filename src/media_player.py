"""
Media-player entity functions.

:copyright: (c) 2023 by Albaintor
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from ucapi import EntityTypes, MediaPlayer, StatusCodes
from ucapi.media_player import (
    Attributes,
    Commands,
    DeviceClasses,
    Features,
    Options,
    States,
)

import avr
from config import DeviceInstance, SonyEntity, create_entity_id
from const import SIMPLE_COMMANDS

_LOG = logging.getLogger(__name__)


class SonyMediaPlayer(MediaPlayer, SonyEntity):
    """Representation of a Sony Media Player entity."""

    def __init__(self, device: DeviceInstance, receiver: avr.SonyDevice):
        """Initialize the class."""
        self._receiver: avr.SonyDevice = receiver

        entity_id = create_entity_id(device.id, EntityTypes.MEDIA_PLAYER)
        features = [
            Features.ON_OFF,
            Features.VOLUME,
            Features.VOLUME_UP_DOWN,
            Features.MUTE_TOGGLE,
            Features.SELECT_SOURCE,
            Features.SELECT_SOUND_MODE,
            Features.MEDIA_ALBUM,
            Features.MEDIA_TITLE,
            Features.MEDIA_ARTIST,
            Features.MEDIA_IMAGE_URL,
            Features.MEDIA_TYPE,
            Features.NEXT,
            Features.PREVIOUS,
            Features.PLAY_PAUSE,
        ]
        attributes = receiver.attributes

        super().__init__(
            entity_id,
            device.name,
            features,
            attributes,
            device_class=DeviceClasses.RECEIVER,
            options={Options.SIMPLE_COMMANDS: SIMPLE_COMMANDS},
        )

    @property
    def deviceid(self) -> str:
        """Return the device identifier."""
        return self._receiver.id

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None, *, websocket: Any) -> StatusCodes:
        """
        Media-player entity command handler.

        Called by the integration-API if a command is sent to a configured media-player entity.

        :param cmd_id: command
        :param params: optional command parameters
        :param websocket: optional websocket connection. Allows for directed event
                          callbacks instead of broadcasts.
        :return: status code of the command request
        """
        _LOG.info("Got %s command request: %s %s", self.id, cmd_id, params)

        if self._receiver is None:
            _LOG.warning("No AVR instance for entity: %s", self.id)
            return StatusCodes.SERVICE_UNAVAILABLE
        res: StatusCodes = StatusCodes.NOT_IMPLEMENTED
        if cmd_id == Commands.VOLUME:
            res = await self._receiver.set_volume_level(params.get("volume"))
        elif cmd_id == Commands.VOLUME_UP:
            res = await self._receiver.volume_up()
        elif cmd_id == Commands.VOLUME_DOWN:
            res = await self._receiver.volume_down()
        elif cmd_id == Commands.MUTE_TOGGLE:
            res = await self._receiver.mute(not self.attributes.get(Attributes.MUTED, False))
        elif cmd_id == Commands.ON:
            res = await self._receiver.power_on()
        elif cmd_id == Commands.OFF:
            res = await self._receiver.power_off()
        elif cmd_id == Commands.SELECT_SOURCE:
            res = await self._receiver.select_source(params.get("source"))
        elif cmd_id == Commands.SELECT_SOUND_MODE:
            res = await self._receiver.select_sound_mode(params.get("mode"))
        elif cmd_id == Commands.NEXT:
            res = await self._receiver.next()
        elif cmd_id == Commands.PREVIOUS:
            res = await self._receiver.previous()
        elif cmd_id == Commands.PLAY_PAUSE:
            res = await self._receiver.play_pause()
        elif cmd_id in self.options[Options.SIMPLE_COMMANDS]:
            if cmd_id == "ZONE_HDMI_OUTPUT_AB":
                res = await self._receiver.set_sound_settings("hdmiOutput", "hdmi_AB")
            elif cmd_id == "ZONE_HDMI_OUTPUT_A":
                res = await self._receiver.set_sound_settings("hdmiOutput", "hdmi_A")
            elif cmd_id == "ZONE_HDMI_OUTPUT_B":
                res = await self._receiver.set_sound_settings("hdmiOutput", "hdim_B")  # Typo in the device software
            elif cmd_id == "ZONE_HDMI_OUTPUT_OFF":
                res = await self._receiver.set_sound_settings("hdmiOutput", "off")
        else:
            return StatusCodes.NOT_IMPLEMENTED

        return res

    def filter_changed_attributes(self, update: dict[str, Any]) -> dict[str, Any]:
        """
        Filter the given attributes and return only the changed values.

        :param update: dictionary with attributes.
        :return: filtered entity attributes containing changed attributes only.
        """
        attributes = {}

        if Attributes.STATE in update:
            state = update[Attributes.STATE]
            attributes = self._key_update_helper(Attributes.STATE, state, attributes)

        for attr in [
            Attributes.MEDIA_ARTIST,
            Attributes.MEDIA_ALBUM,
            Attributes.MEDIA_IMAGE_URL,
            Attributes.MEDIA_TITLE,
            Attributes.MUTED,
            Attributes.SOURCE,
            Attributes.SOURCE,
            Attributes.VOLUME,
        ]:
            if attr in update:
                attributes = self._key_update_helper(attr, update[attr], attributes)

        if Attributes.SOURCE_LIST in update:
            if Attributes.SOURCE_LIST in self.attributes:
                if update[Attributes.SOURCE_LIST] != self.attributes[Attributes.SOURCE_LIST]:
                    attributes[Attributes.SOURCE_LIST] = update[Attributes.SOURCE_LIST]

        if Features.SELECT_SOUND_MODE in self.features:
            if Attributes.SOUND_MODE in update:
                attributes = self._key_update_helper(Attributes.SOUND_MODE, update[Attributes.SOUND_MODE], attributes)
            if Attributes.SOUND_MODE_LIST in update:
                if Attributes.SOUND_MODE_LIST in self.attributes:
                    if update[Attributes.SOUND_MODE_LIST] != self.attributes[Attributes.SOUND_MODE_LIST]:
                        attributes[Attributes.SOUND_MODE_LIST] = update[Attributes.SOUND_MODE_LIST]

        if Attributes.STATE in attributes:
            if attributes[Attributes.STATE] == States.OFF:
                attributes[Attributes.MEDIA_IMAGE_URL] = ""
                attributes[Attributes.MEDIA_ALBUM] = ""
                attributes[Attributes.MEDIA_ARTIST] = ""
                attributes[Attributes.MEDIA_TITLE] = ""
                attributes[Attributes.MEDIA_TYPE] = ""
                attributes[Attributes.SOURCE] = ""

        return attributes

    def _key_update_helper(self, key: str, value: str | None, attributes):
        if value is None:
            return attributes

        if key in self.attributes:
            if self.attributes[key] != value:
                attributes[key] = value
        else:
            attributes[key] = value

        return attributes
