#!/usr/bin/env python3
"""
This module implements a Remote Two integration driver for Sony AVR receivers.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import json
import logging
import os
from typing import Any

import avr
import config
import media_player
import setup_flow
import ucapi
import ucapi.api_definitions as uc
import websockets
from config import avr_from_entity_id
from ucapi import IntegrationAPI
from ucapi.api import filter_log_msg_data
from ucapi.media_player import Attributes as MediaAttr

_LOG = logging.getLogger("driver")  # avoid having __main__ in log messages
_LOOP = asyncio.get_event_loop()

# Global variables
api = ucapi.IntegrationAPI(_LOOP)
# Map of avr_id -> SonyAVR instance
_configured_avrs: dict[str, avr.SonyDevice] = {}
_R2_IN_STANDBY = False


@api.listens_to(ucapi.Events.CONNECT)
async def on_r2_connect_cmd() -> None:
    """Connect all configured receivers when the Remote Two sends the connect command."""
    # TODO check if we were in standby and ignore the call? We'll also get an EXIT_STANDBY
    _LOG.debug("R2 connect command: connecting device(s)")
    for receiver in _configured_avrs.values():
        # start background task
        if receiver.available:
            _LOG.debug("R2 connect : device %s already active", receiver.receiver.endpoint)
            await receiver.connect_event()
            continue
        await receiver.connect()
        await receiver.async_activate_websocket()
    if len(_configured_avrs.values()) == 0:
        await api.set_device_state(ucapi.DeviceStates.CONNECTED)
        # _LOOP.create_task(receiver.connect())


@api.listens_to(ucapi.Events.DISCONNECT)
async def on_r2_disconnect_cmd():
    """Disconnect all configured receivers when the Remote Two sends the disconnect command."""
    # pylint: disable = W0212
    if len(api._clients) == 0:
        for receiver in _configured_avrs.values():
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
    for configured in _configured_avrs.values():
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

    for configured in _configured_avrs.values():
        # start background task
        # pylint: disable = W0212
        if configured.available:
            _LOG.debug(
                "Exit standby event : device %s already active",
                configured._receiver.endpoint,
            )
            continue
        await configured.connect()
        await configured.async_activate_websocket()
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
        avr_id = avr_from_entity_id(entity_id)
        if avr_id in _configured_avrs:
            receiver = _configured_avrs[avr_id]
            state = media_player.state_from_avr(receiver.state)
            api.configured_entities.update_attributes(entity_id, {ucapi.media_player.Attributes.STATE: state})
            continue

        device = config.devices.get(avr_id)
        if device:
            _configure_new_avr(device, connect=True)
        else:
            _LOG.error("Failed to subscribe entity %s: no AVR configuration found", entity_id)


@api.listens_to(ucapi.Events.UNSUBSCRIBE_ENTITIES)
async def on_unsubscribe_entities(entity_ids: list[str]) -> None:
    """On unsubscribe, we disconnect the objects and remove listeners for events."""
    _LOG.debug("Unsubscribe entities event: %s", entity_ids)
    for entity_id in entity_ids:
        avr_id = avr_from_entity_id(entity_id)
        if avr_id is None:
            continue
        if avr_id in _configured_avrs:
            # TODO #21 this doesn't work once we have more than one entity per device!
            # --- START HACK ---
            # Since an AVR instance only provides exactly one media-player, it's save to disconnect if the entity is
            # unsubscribed. This should be changed to a more generic logic, also as template for other integrations!
            # Otherwise this sets a bad copy-paste example and leads to more issues in the future.
            # --> correct logic: check configured_entities, if empty: disconnect
            await _configured_avrs[entity_id].disconnect()
            _configured_avrs[entity_id].events.remove_all_listeners()


async def on_avr_connected(avr_id: str):
    """Handle AVR connection."""
    _LOG.debug("AVR connected: %s", avr_id)

    if avr_id not in _configured_avrs:
        _LOG.warning("AVR %s is not configured", avr_id)
        return

    # TODO #20 when multiple devices are supported, the device state logic isn't that simple anymore!
    await api.set_device_state(ucapi.DeviceStates.CONNECTED)

    for entity_id in _entities_from_avr(avr_id):
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


async def on_avr_disconnected(avr_id: str):
    """Handle AVR disconnection."""
    _LOG.debug("AVR disconnected: %s", avr_id)

    for entity_id in _entities_from_avr(avr_id):
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


async def on_avr_connection_error(avr_id: str, message):
    """Set entities of AVR to state UNAVAILABLE if AVR connection error occurred."""
    _LOG.error(message)

    for entity_id in _entities_from_avr(avr_id):
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


async def handle_avr_address_change(avr_id: str, address: str) -> None:
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


async def on_avr_update(avr_id: str, update: dict[str, Any] | None) -> None:
    """
    Update attributes of configured media-player entity if AVR properties changed.

    :param avr_id: AVR identifier
    :param update: dictionary containing the updated properties or None if
    """
    if update is None:
        if avr_id not in _configured_avrs:
            return
        receiver = _configured_avrs[avr_id]
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
    for entity_id in _entities_from_avr(avr_id):
        configured_entity = api.configured_entities.get(entity_id)
        if configured_entity is None:
            return

        if isinstance(configured_entity, media_player.SonyMediaPlayer):
            attributes = configured_entity.filter_changed_attributes(update)

        if attributes:
            # _LOG.debug("Sony AVR send updated attributes %s %s", entity_id, attributes)
            api.configured_entities.update_attributes(entity_id, attributes)


def _entities_from_avr(avr_id: str) -> list[str]:
    """
    Return all associated entity identifiers of the given AVR.

    :param avr_id: the AVR identifier
    :return: list of entity identifiers
    """
    # dead simple for now: one media_player entity per device!
    # TODO #21 support multiple zones: one media-player per zone
    return [f"media_player.{avr_id}"]


def _configure_new_avr(device: config.AvrDevice, connect: bool = True) -> None:
    """
    Create and configure a new AVR device.

    Supported entities of the device are created and registered in the integration library as available entities.

    :param device: the receiver configuration.
    :param connect: True: start connection to receiver.
    """
    # the device should not yet be configured, but better be safe
    if device.id in _configured_avrs:
        receiver = _configured_avrs[device.id]
        _LOOP.create_task(receiver.disconnect())
    else:
        receiver = avr.SonyDevice(device, loop=_LOOP)

        receiver.events.on(avr.Events.CONNECTED, on_avr_connected)
        receiver.events.on(avr.Events.DISCONNECTED, on_avr_disconnected)
        receiver.events.on(avr.Events.ERROR, on_avr_connection_error)
        receiver.events.on(avr.Events.UPDATE, on_avr_update)
        # receiver.events.on(avr.Events.IP_ADDRESS_CHANGED, handle_avr_address_change)
        # receiver.connect()
        _configured_avrs[device.id] = receiver

    if connect:
        # start background connection task
        # _LOOP.create_task(receiver.connect())
        receiver.async_activate_websocket()

    _register_available_entities(device, receiver)


def _register_available_entities(device: config.AvrDevice, receiver: avr.SonyDevice) -> None:
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


def on_device_added(device: config.AvrDevice) -> None:
    """Handle a newly added device in the configuration."""
    _LOG.debug("New device added: %s", device)
    _configure_new_avr(device, connect=False)


def on_device_removed(device: config.AvrDevice | None) -> None:
    """Handle a removed device in the configuration."""
    if device is None:
        _LOG.debug("Configuration cleared, disconnecting & removing all configured AVR instances")
        for configured in _configured_avrs.values():
            _LOOP.create_task(_async_remove(configured))
        _configured_avrs.clear()
        api.configured_entities.clear()
        api.available_entities.clear()
    else:
        if device.id in _configured_avrs:
            _LOG.debug("Disconnecting from removed AVR %s", device.id)
            configured = _configured_avrs.pop(device.id)
            _LOOP.create_task(_async_remove(configured))
            for entity_id in _entities_from_avr(configured.id):
                api.configured_entities.remove(entity_id)
                api.available_entities.remove(entity_id)


async def _async_remove(receiver: avr.SonyDevice) -> None:
    """Disconnect from receiver and remove all listeners."""
    await receiver.disconnect()
    receiver.events.remove_all_listeners()


async def patched_broadcast_ws_event(self, msg: str, msg_data: dict[str, Any], category: uc.EventCategory) -> None:
    """
    Send the given event-message to all connected WebSocket clients.

    If a client is no longer connected, a log message is printed and the remaining
    clients are notified.

    :param msg: event message name
    :param msg_data: message data payload
    :param category: event category
    """
    data = {"kind": "event", "msg": msg, "msg_data": msg_data, "cat": category}
    data_dump = json.dumps(data)
    data_log = None
    # filter fields
    if _LOG.isEnabledFor(logging.DEBUG):
        data_log = json.dumps(data) if filter_log_msg_data(data) else data_dump
    # pylint: disable = W0212
    for websocket in self._clients.copy():
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("[%s] ->: %s", websocket.remote_address, data_log)
        try:
            await websocket.send(data_dump)
        except websockets.exceptions.WebSocketException:
            pass


async def main():
    """Start the Remote Two integration driver."""
    logging.basicConfig()
    level = os.getenv("UC_LOG_LEVEL", "DEBUG").upper()
    logging.getLogger("avr").setLevel(level)
    logging.getLogger("discover").setLevel(level)
    logging.getLogger("driver").setLevel(level)
    logging.getLogger("media_player").setLevel(level)
    logging.getLogger("receiver").setLevel(level)
    logging.getLogger("setup_flow").setLevel(level)

    config.devices = config.Devices(api.config_dir_path, on_device_added, on_device_removed)
    for device in config.devices.all():
        _configure_new_avr(device, connect=False)

    # _LOOP.create_task(receiver_status_poller())
    for receiver in _configured_avrs.values():
        if receiver.available:
            _LOG.debug("Main driver : device %s already active", receiver.receiver.endpoint)
            continue
        await receiver.connect()
        await receiver.async_activate_websocket()
    # pylint: disable = W0212
    IntegrationAPI._broadcast_ws_event = patched_broadcast_ws_event
    await api.init("driver.json", setup_flow.driver_setup_handler)


if __name__ == "__main__":
    _LOOP.run_until_complete(main())
    _LOOP.run_forever()
