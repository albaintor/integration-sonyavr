#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Sony AVR receivers.

:copyright: (c) 2023 by Albaintor
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
import os
import sys
from typing import Any

import ucapi
from ucapi.media_player import Attributes as MediaAttr

import avr
import config
import media_player
import setup_flow
from config import device_from_entity_id

_LOG = logging.getLogger("driver")  # avoid having __main__ in log messages
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)
# Global variables
api = ucapi.IntegrationAPI(_LOOP)
# Map of avr_id -> SonyAVR instance
_configured_devices: dict[str, avr.SonyDevice] = {}
_R2_IN_STANDBY = False


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured receivers when the Remote Two sends the connect command."""
    # TODO check if we were in standby and ignore the call? We'll also get an EXIT_STANDBY
    _LOG.debug("R2 connect command: connecting device(s)")
    for receiver in _configured_devices.values():
        # start background task
        if receiver.available:
            _LOG.debug("R2 connect : device %s already active", receiver.receiver.endpoint)
            await receiver.connect_event()
            continue
        await receiver.connect()
    if len(_configured_devices.values()) == 0:
        await api.set_device_state(ucapi.DeviceStates.CONNECTED)
        # _LOOP.create_task(receiver.connect())


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured receivers when the Remote Two sends the disconnect command."""
    # pylint: disable = W0212
    _LOG.debug("Remote requests disconnection")
    if len(api._clients) == 0:
        for receiver in _configured_devices.values():
            # start background task
            await receiver.disconnect()
            # _LOOP.create_task(receiver.disconnect())


@api.listens_to(ucapi.Events.ENTER_STANDBY)
async def on_r2_enter_standby() -> None:
    """
    Enter standby notification from Remote Two.

    Disconnect every Sony AVR instances.
    """
    global _R2_IN_STANDBY

    _R2_IN_STANDBY = True
    _LOG.debug("Enter standby event: disconnecting device(s)")
    for configured in _configured_devices.values():
        await configured.disconnect()


@api.listens_to(ucapi.Events.EXIT_STANDBY)
async def on_r2_exit_standby() -> None:
    """
    Exit standby notification from Remote Two.

    Connect all Sony AVR instances.
    """
    global _R2_IN_STANDBY

    _R2_IN_STANDBY = False
    _LOG.debug("Exit standby event: connecting device(s)")

    for configured in _configured_devices.values():
        # start background task
        # pylint: disable = W0212
        if configured.available:
            _LOG.debug(
                "Exit standby event : device %s already active",
                configured._receiver.endpoint,
            )
            continue
        await configured.connect()
        # _LOOP.create_task(configured.connect())


@api.listens_to(ucapi.Events.SUBSCRIBE_ENTITIES)
async def on_subscribe_entities(entity_ids: list[str]) -> None:
    """
    Subscribe to given entities.

    :param entity_ids: entity identifiers.
    """
    global _R2_IN_STANDBY

    _R2_IN_STANDBY = False
    _LOG.debug("Subscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        avr_id = device_from_entity_id(entity_id)
        if avr_id in _configured_devices:
            receiver = _configured_devices[avr_id]
            attributes = receiver.attributes
            api.configured_entities.update_attributes(entity_id, attributes)
            continue

        device = config.devices.get(avr_id)
        if device:
            _configure_new_device(device, connect=True)
        else:
            _LOG.error("Failed to subscribe entity %s: no AVR configuration found", entity_id)


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    _LOG.debug("Unsubscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        device_id = device_from_entity_id(entity_id)
        if device_id is None:
            continue
        if device_id in _configured_devices:
            # TODO #21 this doesn't work once we have more than one entity per device!
            # --- START HACK ---
            # Since an AVR instance only provides exactly one media-player, it's save to disconnect if the entity is
            # unsubscribed. This should be changed to a more generic logic, also as template for other integrations!
            # Otherwise this sets a bad copy-paste example and leads to more issues in the future.
            # --> correct logic: check configured_entities, if empty: disconnect
            await _configured_devices[entity_id].disconnect()
            _configured_devices[entity_id].events.remove_all_listeners()


async def on_device_connected(device_id: str):
    """Handle Device connection."""
    _LOG.debug("Device connected: %s", device_id)

    if device_id not in _configured_devices:
        _LOG.warning("Device %s is not configured", device_id)
        return

    # TODO #20 when multiple devices are supported, the device state logic isn't that simple anymore!
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)

    for entity_id in _entities_from_device(device_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            continue

        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            if (
                configured_entity.attributes[ucapi.media_player.Attributes.STATE]
                == ucapi.media_player.States.UNAVAILABLE
            ):
                # TODO why STANDBY?
                api.configured_entities.update_attributes(
                    entity_id,
                    {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.STANDBY},
                )


async def on_device_disconnected(avr_id: str):
    """Handle Device disconnection."""
    _LOG.debug("Device disconnected: %s", avr_id)

    for entity_id in _entities_from_device(avr_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            continue

        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            api.configured_entities.update_attributes(
                entity_id,
                {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE},
            )

    # TODO #20 when multiple devices are supported, the device state logic isn't that simple anymore!
    await api.set_device_state(ucapi.DeviceStates.DISCONNECTED)


async def on_device_connection_error(avr_id: str, message):
    """Set entities of Device to state UNAVAILABLE if AVR connection error occurred."""
    _LOG.error(message)

    for entity_id in _entities_from_device(avr_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            continue

        if configured_entity.entity_type == ucapi.EntityTypes.MEDIA_PLAYER:
            api.configured_entities.update_attributes(
                entity_id,
                {ucapi.media_player.Attributes.STATE: ucapi.media_player.States.UNAVAILABLE},
            )

    # TODO #20 when multiple devices are supported, the device state logic isn't that simple anymore!
    await api.set_device_state(ucapi.DeviceStates.ERROR)


async def handle_device_address_change(avr_id: str, address: str) -> None:
    """Update device configuration with changed IP address."""
    device = config.devices.get(avr_id)
    if device and device.address != address:
        _LOG.info(
            "Updating IP address of configured AVR %s: %s -> %s",
            avr_id,
            device.address,
            address,
        )
        device.address = address
        config.devices.update(device)


async def on_device_update(avr_id: str, update: dict[str, Any] | None) -> None:
    """
    Update attributes of configured media-player entity if AVR properties changed.

    :param avr_id: AVR identifier
    :param update: dictionary containing the updated properties or None if
    """
    if update is None:
        if avr_id not in _configured_devices:
            return
        receiver = _configured_devices[avr_id]
        update = {
            MediaAttr.STATE: receiver.state,
            MediaAttr.MEDIA_ARTIST: receiver.media_artist,
            MediaAttr.MEDIA_ALBUM: receiver.media_album_name,
            MediaAttr.MEDIA_IMAGE_URL: receiver.media_image_url,
            MediaAttr.MEDIA_TITLE: receiver.media_title,
            MediaAttr.MUTED: receiver.is_volume_muted,
            MediaAttr.SOURCE: receiver.source,
            MediaAttr.SOURCE_LIST: receiver.source_list,
            MediaAttr.SOUND_MODE: receiver.sound_mode,
            MediaAttr.SOUND_MODE_LIST: receiver.sound_mode_list,
            MediaAttr.VOLUME: receiver.volume_level,
        }
    else:
        _LOG.info("[%s] AVR update: %s", avr_id, update)

    attributes = None

    # TODO awkward logic: this needs better support from the integration library
    for entity_id in _entities_from_device(avr_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            return

        if isinstance(configured_entity, media_player.SonyMediaPlayer):
            attributes = configured_entity.filter_changed_attributes(update)

        if attributes:
            # _LOG.debug("Sony AVR send updated attributes %s %s", entity_id, attributes)
            api.configured_entities.update_attributes(entity_id, attributes)


def _entities_from_device(device_id: str) -> list[str]:
    """
    Return all associated entity identifiers of the given AVR.

    :param device_id: the AVR identifier
    :return: list of entity identifiers
    """
    # dead simple for now: one media_player entity per device!
    # TODO #21 support multiple zones: one media-player per zone
    return [f"media_player.{device_id}"]


def _configure_new_device(device: config.DeviceInstance, connect: bool = True) -> None:
    """
    Create and configure a new AVR device.

    Supported entities of the device are created and registered in the integration library as available entities.

    :param device: the receiver configuration.
    :param connect: True: start connection to receiver.
    """
    # the device should not yet be configured, but better be safe
    if device.id in _configured_devices:
        receiver = _configured_devices[device.id]
        _LOOP.create_task(receiver.disconnect())
    else:
        receiver = avr.SonyDevice(device, loop=_LOOP)

        receiver.events.on(avr.Events.CONNECTED, on_device_connected)
        receiver.events.on(avr.Events.DISCONNECTED, on_device_disconnected)
        receiver.events.on(avr.Events.ERROR, on_device_connection_error)
        receiver.events.on(avr.Events.UPDATE, on_device_update)
        # receiver.events.on(avr.Events.IP_ADDRESS_CHANGED, handle_avr_address_change)
        # receiver.connect()
        _configured_devices[device.id] = receiver

    if connect:
        # start background connection task
        _LOOP.create_task(receiver.connect())

    _register_available_entities(device, receiver)


def _register_available_entities(device: config.DeviceInstance, receiver: avr.SonyDevice) -> None:
    """
    Create entities for given receiver device and register them as available entities.

    :param device: Receiver
    """
    # plain and simple for now: only one media_player per AVR device
    # entity = media_player.create_entity(device)
    entity = media_player.SonyMediaPlayer(device, receiver)

    if api.available_entities.contains(entity.id):
        api.available_entities.remove(entity.id)
    api.available_entities.add(entity)


def on_device_added(device: config.DeviceInstance) -> None:
    """Handle a newly added device in the configuration."""
    _LOG.debug("New device added: %s", device)
    _configure_new_device(device, connect=False)


def on_device_updated(device: config.DeviceInstance) -> None:
    """Handle an updated device in the configuration."""
    _LOG.debug("Device config updated: %s, reconnect with new configuration", device)
    if device.id in _configured_devices:
        _LOG.debug("Disconnecting from removed device %s", device.id)
        configured = _configured_devices.pop(device.id)
        configured.events.remove_all_listeners()
        for entity_id in _entities_from_device(configured.id):
            api.configured_entities.remove(entity_id)
            api.available_entities.remove(entity_id)
    _configure_new_device(device, connect=True)


def on_device_removed(device: config.DeviceInstance | None) -> None:
    """Handle a removed device in the configuration."""
    if device is None:
        _LOG.debug("Configuration cleared, disconnecting & removing all configured AVR instances")
        for configured in _configured_devices.values():
            _LOOP.create_task(_async_remove(configured))
        _configured_devices.clear()
        api.configured_entities.clear()
        api.available_entities.clear()
    else:
        if device.id in _configured_devices:
            _LOG.debug("Disconnecting from removed AVR %s", device.id)
            configured = _configured_devices.pop(device.id)
            _LOOP.create_task(_async_remove(configured))
            for entity_id in _entities_from_device(configured.id):
                api.configured_entities.remove(entity_id)
                api.available_entities.remove(entity_id)


async def _async_remove(receiver: avr.SonyDevice) -> None:
    """Disconnect from receiver and remove all listeners."""
    await receiver.disconnect()
    receiver.events.remove_all_listeners()


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()
    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("avr").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    logging.getLogger("config").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed, on_device_updated)
    for device in config.devices.all():
        _configure_new_device(device, connect=False)

    await _LOOP.create_task(config.devices.handle_address_change())

    # _LOOP.create_task(receiver_status_poller())
    for receiver in _configured_devices.values():
        if receiver.available:
            _LOG.debug("Main driver : device %s already active", receiver.receiver.endpoint)
            continue
        await receiver.connect()
    await api.init("driver.json", setup_flow.driver_setup_handler)


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
