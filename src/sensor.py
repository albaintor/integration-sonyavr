"""
Media-player entity functions.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

import ucapi.media_player
from ucapi import EntityTypes, Sensor
from ucapi.media_player import States as MediaStates
from ucapi.sensor import Attributes, DeviceClasses, Options, States

import avr
from config import DeviceInstance, SonyEntity, create_entity_id
from const import SonySensors

_LOG = logging.getLogger(__name__)

SENSOR_STATE_MAPPING = {
    MediaStates.OFF: States.UNAVAILABLE,
    MediaStates.ON: States.ON,
    MediaStates.STANDBY: States.ON,
    MediaStates.PLAYING: States.ON,
    MediaStates.PAUSED: States.ON,
    MediaStates.UNAVAILABLE: States.UNAVAILABLE,
    MediaStates.UNKNOWN: States.UNKNOWN,
}


# pylint: disable=R0917
class SonySensor(SonyEntity, Sensor):
    """Representation of a Sony Sensor entity."""

    def __init__(
        self,
        entity_id: str,
        name: str | dict[str, str],
        config_device: DeviceInstance,
        device: avr.SonyDevice,
        options: dict[Options, Any] | None = None,
        device_class: DeviceClasses = DeviceClasses.CUSTOM,
    ) -> None:
        """Initialize the class."""
        # pylint: disable = R0801
        self._device: avr.SonyDevice = device
        features = []
        attributes = dict[Any, Any]()
        self._config_device = config_device
        self._state: States = States.UNAVAILABLE
        super().__init__(entity_id, name, features, attributes, device_class=device_class, options=options)

    @property
    def deviceid(self) -> str:
        """Return the device identifier."""
        return self._device.id

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the updated attributes of current sensor entity."""
        raise NotImplementedError()


class SonySensorVolume(SonySensor):
    """Current volume sensor entity."""

    ENTITY_NAME = "sensor_volume"

    def __init__(self, config_device: DeviceInstance, device: avr.SonyDevice):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.id, EntityTypes.SENSOR)}.{SonySensorVolume.ENTITY_NAME}"
        # TODO : dict instead of name to report language names
        self._device = device
        self._config_device = config_device
        super().__init__(entity_id, {"en": "Volume", "fr": "Volume"}, config_device, device)

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated sensor value from full update if provided or sensor value if no udpate is provided."""
        attributes: dict[str, Any] = {}
        if update:
            if ucapi.media_player.Attributes.STATE in update:
                attributes[Attributes.STATE] = SENSOR_STATE_MAPPING.get(update[ucapi.media_player.Attributes.STATE])
            if SonySensors.SENSOR_VOLUME in update:
                attributes[Attributes.VALUE] = update[SonySensors.SENSOR_VOLUME]
            return attributes
        return {
            Attributes.VALUE: self._device.volume_level,
            Attributes.STATE: SENSOR_STATE_MAPPING.get(self._device.state),
        }


class SonySensorMuted(SonySensor):
    """Current mute state sensor entity."""

    ENTITY_NAME = "sensor_muted"

    def __init__(self, config_device: DeviceInstance, device: avr.SonyDevice):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.id, EntityTypes.SENSOR)}.{SonySensorMuted.ENTITY_NAME}"
        self._device = device
        self._config_device = config_device
        super().__init__(
            entity_id, {"en": "Muted", "fr": "Son coupé"}, config_device, device, None, DeviceClasses.BINARY
        )

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated sensor value from full update if provided or sensor value if no udpate is provided."""
        attributes: dict[str, Any] = {}
        if update:
            if ucapi.media_player.Attributes.STATE in update:
                attributes[Attributes.STATE] = SENSOR_STATE_MAPPING.get(update[ucapi.media_player.Attributes.STATE])
            if SonySensors.SENSOR_MUTED in update:
                attributes[Attributes.VALUE] = update[SonySensors.SENSOR_MUTED]
            return attributes
        return {
            Attributes.VALUE: self._device.is_volume_muted,
            Attributes.STATE: SENSOR_STATE_MAPPING.get(self._device.state),
        }


class SonySensorInputSource(SonySensor):
    """Current input source entity."""

    ENTITY_NAME = "sensor_input"

    def __init__(self, config_device: DeviceInstance, device: avr.SonyDevice):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.id, EntityTypes.SENSOR)}.{SonySensorInputSource.ENTITY_NAME}"
        # TODO : dict instead of name to report language names
        self._device = device
        self._config_device = config_device
        super().__init__(entity_id, {"en": "Input", "fr": "Entrée"}, config_device, device)

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated sensor value from full update if provided or sensor value if no udpate is provided."""
        attributes: dict[str, Any] = {}
        if update:
            if ucapi.media_player.Attributes.STATE in update:
                attributes[Attributes.STATE] = SENSOR_STATE_MAPPING.get(update[ucapi.media_player.Attributes.STATE])
            if SonySensors.SENSOR_INPUT in update:
                attributes[Attributes.VALUE] = update[SonySensors.SENSOR_INPUT]
            return attributes
        return {
            Attributes.VALUE: self._device.source,
            Attributes.STATE: SENSOR_STATE_MAPPING.get(self._device.state),
        }


class SonySensorSoundMode(SonySensor):
    """Current sound mode sensor entity."""

    ENTITY_NAME = "sensor_sound_mode"

    def __init__(self, config_device: DeviceInstance, device: avr.SonyDevice):
        """Initialize the class."""
        entity_id = f"{create_entity_id(config_device.id, EntityTypes.SENSOR)}.{SonySensorSoundMode.ENTITY_NAME}"
        # TODO : dict instead of name to report language names
        self._device = device
        self._config_device = config_device
        super().__init__(entity_id, {"en": "Sound mode", "fr": "Mode sonore"}, config_device, device)

    def update_attributes(self, update: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Return updated sensor value from full update if provided or sensor value if no udpate is provided."""
        attributes: dict[str, Any] = {}
        if update:
            if ucapi.media_player.Attributes.STATE in update:
                attributes[Attributes.STATE] = SENSOR_STATE_MAPPING.get(update[ucapi.media_player.Attributes.STATE])
            if SonySensors.SENSOR_SOUND_MODE in update:
                attributes[Attributes.VALUE] = update[SonySensors.SENSOR_SOUND_MODE]
            return attributes
        return {
            Attributes.VALUE: self._device.sound_mode,
            Attributes.STATE: SENSOR_STATE_MAPPING.get(self._device.state),
        }
