"""
Media-player entity functions.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import avr
from config import AvrDevice, create_entity_id
from ucapi import EntityTypes, MediaPlayer, StatusCodes
from ucapi.media_player import Attributes, Commands, DeviceClasses, Features, States

_LOG = logging.getLogger(__name__)


class SonyMediaPlayer(MediaPlayer):
    """Representation of a Sony Media Player entity."""

    def __init__(self, device: AvrDevice, receiver: avr.SonyDevice):
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
        attributes = {
            Attributes.STATE: receiver.state,
            Attributes.VOLUME: receiver.volume_level,
            Attributes.MUTED: receiver.is_volume_muted,
            Attributes.SOURCE: receiver.source if receiver.source else "",
            Attributes.SOURCE_LIST: (receiver.source_list if receiver.source_list else []),
            Attributes.SOUND_MODE: receiver.sound_mode,
            Attributes.SOUND_MODE_LIST: receiver.sound_mode_list,
            Attributes.MEDIA_IMAGE_URL: receiver.media_image_url,
            Attributes.MEDIA_TITLE: receiver.media_title,
            Attributes.MEDIA_ARTIST: receiver.media_artist,
            Attributes.MEDIA_ALBUM: receiver.media_album_name,
        }
        # # use sound mode support & name from configuration: receiver might not yet be connected
        # if device.support_sound_mode:
        #     features.append(Features.SELECT_SOUND_MODE)
        #     attributes[Attributes.SOUND_MODE] = ""
        #     attributes[Attributes.SOUND_MODE_LIST] = []

        super().__init__(
            entity_id,
            device.name,
            features,
            attributes,
            device_class=DeviceClasses.RECEIVER,
        )

    async def command(self, cmd_id: str, params: dict[str, Any] | None = None) -> StatusCodes:
        """
        Media-player entity command handler.

        Called by the integration-API if a command is sent to a configured media-player entity.

        :param cmd_id: command
        :param params: optional command parameters
        :return: status code of the command request
        """
        _LOG.info("Got %s command request: %s %s", self.id, cmd_id, params)

        if self._receiver is None:
            _LOG.warning("No AVR instance for entity: %s", self.id)
            return StatusCodes.SERVICE_UNAVAILABLE

        if cmd_id == Commands.VOLUME:
            res = await self._receiver.set_volume_level(params.get("volume"))
        elif cmd_id == Commands.VOLUME_UP:
            res = await self._receiver.volume_up()
        elif cmd_id == Commands.VOLUME_DOWN:
            res = await self._receiver.volume_down()
        elif cmd_id == Commands.MUTE_TOGGLE:
            res = await self._receiver.mute(not self.attributes[Attributes.MUTED])
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
