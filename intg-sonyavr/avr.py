"""
This module implements the AVR AVR receiver communication of the Remote Two integration driver.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from asyncio import AbstractEventLoop, CancelledError, Lock
from collections import OrderedDict
from enum import IntEnum
from functools import wraps
from typing import Any, Awaitable, Callable, Concatenate, Coroutine, ParamSpec, TypeVar

import ucapi
from config import AvrDevice
from pyee.asyncio import AsyncIOEventEmitter
from songpal import (
    ConnectChange,
    ContentChange,
    Device,
    PowerChange,
    SongpalException,
    VolumeChange,
)
from songpal.containers import InterfaceInfo, PlayInfo, Setting, Sysinfo
from ucapi.media_player import Attributes as MediaAttr, States

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
VOLUME_STEP = 2

BACKOFF_MAX: float = 30
MIN_RECONNECT_DELAY: float = 0.5
BACKOFF_FACTOR: float = 1.5

DISCOVERY_AFTER_CONNECTION_ERRORS = 10

_SonyDeviceT = TypeVar("_SonyDeviceT", bound="SonyDevice")
_P = ParamSpec("_P")


class Events(IntEnum):
    """Internal driver events."""

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    ERROR = 3
    UPDATE = 4
    # IP_ADDRESS_CHANGED = 6


SONY_PLAYBACK_STATE_MAPPING = {
    "STOPPED": States.ON,
    "PLAYING": States.PLAYING,
    "PAUSED": States.PAUSED,
}


# TODO : use wrapper for commands, but to be confirmed as there is a reconnect task
def cmd_wrapper(
    func: Callable[Concatenate[_SonyDeviceT, _P], Awaitable[ucapi.StatusCodes | None]],
) -> Callable[Concatenate[_SonyDeviceT, _P], Coroutine[Any, Any, ucapi.StatusCodes | None]]:
    """Catch command exceptions."""

    @wraps(func)
    async def wrapper(obj: _SonyDeviceT, *args: _P.args, **kwargs: _P.kwargs) -> ucapi.StatusCodes:
        """Wrap all command methods."""
        # pylint: disable = W0212
        try:
            # Reconnects if device was off
            if not obj._always_active and obj._websocket_task is None:
                await obj.reconnect()
            await func(obj, *args, **kwargs)
            return ucapi.StatusCodes.OK
        except SongpalException as exc:
            # If Kodi is off, we expect calls to fail.
            if obj.state == States.OFF:
                log_function = _LOG.debug
            else:
                log_function = _LOG.error
            log_function(
                "Error calling %s on [%s(%s)]: %r trying to reconnect and send the command next",
                func.__name__,
                obj._name,
                obj._receiver.endpoint,
                exc,
            )
            # AVR not connected, launch a connect task but
            # don't wait more than 5 seconds, then process the command if connected
            # else returns error
            connect_task = obj.event_loop.create_task(obj.connect())
            await asyncio.sleep(0)
            try:
                async with asyncio.timeout(5):
                    await connect_task
            except asyncio.TimeoutError:
                log_function("Timeout for reconnect, command won't be sent")
            else:
                if obj._available:
                    try:
                        await func(obj, *args, **kwargs)
                        return ucapi.StatusCodes.OK
                    except SongpalException as ex:
                        log_function(
                            "Error calling %s on [%s(%s)]: %r trying to reconnect",
                            func.__name__,
                            obj._name,
                            obj._receiver.endpoint,
                            ex,
                        )
            # If AVR is off, we expect calls to fail.
            # await obj.event_loop.create_task(obj.connect())
            return ucapi.StatusCodes.BAD_REQUEST
        # pylint: disable = W0718
        except Exception as ex:
            _LOG.error("Unknown error %s %s", func.__name__, ex)
            return ucapi.StatusCodes.BAD_REQUEST

    return wrapper


class SonyDevice:
    """Representing a Sony AVR Device."""

    def __init__(
        self,
        device: AvrDevice,
        loop: AbstractEventLoop | None = None,
    ):
        """Create instance with given IP or hostname of AVR."""
        # identifier from configuration
        self.id: str = device.id
        # friendly name from configuration
        self._name: str = device.name
        self._always_active = device.always_on
        self.event_loop = loop or asyncio.get_running_loop()
        self.events = AsyncIOEventEmitter(self.event_loop)
        self._receiver: Device = Device(device.address)
        self._available: bool = False

        self._connecting: bool = False
        self._connection_attempts: int = 0
        self._reconnect_delay: float = MIN_RECONNECT_DELAY
        self._getting_data: bool = False

        self._interface_info: InterfaceInfo | None = None
        self._sysinfo: Sysinfo | None = None
        self._volume_control = None
        self._volume_min = 0
        self._volume_max = 1
        self._volume = 0
        self._attr_is_volume_muted = False
        self._active_source = None
        self._sources = {}
        self._powered = False
        self._playback_state = States.UNKNOWN
        self._state = States.UNKNOWN
        self._sound_fields: Setting | None = None
        self._play_info: list[PlayInfo] | None = None
        self._unique_id: str | None = None
        self._websocket_task = None
        self._websocket_connect_lock = Lock()
        self._connect_lock = Lock()
        self._check_device_task = None
        _LOG.debug(
            "Sony AVR created: %s (%s), connection keep alive = %s",
            device.name,
            device.address,
            device.always_on,
        )

    async def _init_websocket(self):
        # Start websocket
        _LOG.debug(
            "Sony AVR  [%s(%s)] Initializing websocket",
            self._name,
            self._receiver.endpoint,
        )
        if self._websocket_task:
            try:
                self._websocket_task.cancel()
                await self._receiver.stop_listen_notifications()
            # pylint: disable = W0718
            except Exception:
                pass
            finally:
                self._websocket_task = None
        self._websocket_task = self.event_loop.create_task(self._receiver.listen_notifications())
        _LOG.info(
            "Sony AVR  [%s(%s)] Websocket initialized",
            self._name,
            self._receiver.endpoint,
        )
        _LOG.debug("", exc_info=True)

    async def reconnect(self):
        """Reconnect to device."""
        _LOG.warning(
            "Sony AVR  [%s(%s)] Got disconnected, trying to reconnect",
            self._name,
            self._receiver.endpoint,
        )
        self._available = False
        self._state = States.UNKNOWN
        self._notify_updated_data()

        # Try to reconnect forever, a successful reconnect will initialize
        # the websocket connection again.
        delay = DISCOVERY_AFTER_CONNECTION_ERRORS
        while not self._available:
            _LOG.debug("Sony AVR Trying to reconnect every %s seconds", delay)
            # self.events.emit(Events.CONNECTING, self.id)
            try:
                async with asyncio.timeout(5):
                    task = asyncio.create_task(self._receiver.get_supported_methods())
                    await task
            except (asyncio.TimeoutError, SongpalException) as ex:
                _LOG.debug("Sony AVR Failed to reconnect: %s", ex)
                delay = min(2 * delay, 300)
                if task:
                    try:
                        task.cancel()
                        task = None
                    except CancelledError:
                        pass
            else:
                # We need to inform Remote about the state in case we are coming
                # back from a disconnected state and update internal data
                _LOG.debug("Sony AVR replied, connecting...")
                await self.connect()
                _LOG.debug("Sony AVR replied, connection : %s", self._available)
                # self._notify_updated_data()
            if not self._available:
                await asyncio.sleep(delay)
        _LOG.debug("Sony AVR reconnected, init websocket...")
        await self._init_websocket()
        _LOG.warning(
            "Sony AVR [%s(%s)] Connection reestablished",
            self._name,
            self._receiver.endpoint,
        )

    async def async_activate_websocket(self):
        """Activate websocket for listening if wanted."""
        # pylint: disable = R0915
        _LOG.debug("async_activate_websocket", exc_info=True)

        async def _volume_changed(volume: VolumeChange):
            _LOG.debug("Sony AVR volume changed: %s", volume)
            attr_changed = {}
            if self._volume != volume.volume:
                self._volume = volume.volume
                attr_changed[MediaAttr.VOLUME] = self.volume_level
            if self._attr_is_volume_muted != volume.mute:
                self._attr_is_volume_muted = volume.mute
                attr_changed[MediaAttr.MUTED] = self._attr_is_volume_muted
            if attr_changed:
                self.events.emit(Events.UPDATE, self.id, attr_changed)

        async def _source_changed(content: ContentChange):
            _LOG.debug("Sony AVR Source changed: %s", content)
            self._play_info = [content]
            updated_data = {}
            if content.state and SONY_PLAYBACK_STATE_MAPPING.get(content.state, None):
                self._playback_state = SONY_PLAYBACK_STATE_MAPPING.get(content.state)
                if self.update_state():
                    updated_data[MediaAttr.STATE] = self.state

            if content.is_input:
                self._active_source = self._sources[content.uri]
                _LOG.debug("Sony AVR New active source: %s", self._active_source)
                updated_data[MediaAttr.SOURCE] = self.source
                self.events.emit(Events.UPDATE, self.id, updated_data)
            elif bool(updated_data):
                self.events.emit(Events.UPDATE, self.id, updated_data)

        async def _wait_power_on():
            max_checks = 10
            check_number = 0
            while True:
                await asyncio.sleep(10)
                if self.state == States.ON:
                    _LOG.debug("Device %s is on again", self.id)
                    break
                check_number += 1
                _LOG.debug("Device %s is off check number %s", self.id, check_number)
                if check_number > max_checks:
                    _LOG.debug("Device %s is still off, disconnect all", self.id)
                    await self.close_connections()
                    break
            self._check_device_task = None

        async def _power_changed(power: PowerChange):
            _LOG.debug("Sony AVR Power changed: %s", power)
            self._powered = power.status
            if self.update_state():
                self.events.emit(Events.UPDATE, self.id, {MediaAttr.STATE: self._state})
            if self.state == States.OFF and not self._always_active:
                if self._check_device_task is None:
                    self._check_device_task = self.event_loop.create_task(_wait_power_on())
            elif self.state not in [States.UNKNOWN, States.UNAVAILABLE] and self._check_device_task:
                try:
                    self._check_device_task.cancel()
                except CancelledError:
                    pass
                self._check_device_task = None

        async def _try_reconnect(connect: ConnectChange):
            _LOG.debug("Disconnected: %s", connect.exception)
            await self.reconnect()

        _LOG.info("Sony AVR Activating websocket connection")
        if self._websocket_connect_lock.locked():
            _LOG.info("Sony AVR Activating websocket already initializing, abort")
            return
        await self._websocket_connect_lock.acquire()
        try:
            self._receiver.clear_notification_callbacks()
            self._receiver.on_notification(VolumeChange, _volume_changed)
            self._receiver.on_notification(ContentChange, _source_changed)
            self._receiver.on_notification(PowerChange, _power_changed)
            self._receiver.on_notification(ConnectChange, _try_reconnect)
            await self._init_websocket()
        # pylint: disable = W0718
        except Exception as ex:
            _LOG.info(
                "Sony AVR Unknown error during websocket initialization %s. Please report",
                ex,
            )
        finally:
            _LOG.info("Sony AVR websocket connection initialized")
            self._websocket_connect_lock.release()

    async def connect_event(self):
        """Connect event."""
        self.events.emit(Events.CONNECTED, self.id)
        self._notify_updated_data()

    async def connect(self):
        """Connect to device."""
        # pylint: disable = R0915
        try:
            if self._connect_lock.locked():
                _LOG.info("Sony AVR connection already in progress")
                return
            _LOG.info("Sony AVR connect...")
            await self._connect_lock.acquire()
            self._connecting = True
            await self._receiver.get_supported_methods()
            if self._interface_info is None:
                self._interface_info = await self._receiver.get_interface_information()
            if self._sysinfo is None:
                self._sysinfo = await self._receiver.get_system_info()

            self._unique_id = self._sysinfo.serialNumber
            if self._unique_id is None:
                self._unique_id = self._sysinfo.macAddr
            if self._unique_id is None:
                self._unique_id = self._sysinfo.wirelessMacAddr

            settings = await self._receiver.get_sound_settings("soundField")
            if settings and len(settings) > 0:
                self._sound_fields = settings[0]
            else:
                self._sound_fields = None

            volumes = await self._receiver.get_volume_information()
            if not volumes:
                _LOG.error("Sony AVR Got no volume controls, bailing out")
                self._available = False
                return

            if len(volumes) > 1:
                _LOG.debug("Sony AVR Got %s volume controls, using the first one", volumes)

            volume = volumes[0]
            self._volume_max = volume.maxVolume
            self._volume_min = volume.minVolume
            self._volume = volume.volume
            self._volume_control = volume
            self._attr_is_volume_muted = self._volume_control.is_muted

            status = await self._receiver.get_power()
            self._powered = status.status
            _LOG.debug("Got state: %s", status)

            inputs = await self._receiver.get_inputs()
            _LOG.debug("Got ins: %s", inputs)

            self._sources = OrderedDict()
            for input_ in inputs:
                self._sources[input_.uri] = input_
                if input_.active:
                    self._active_source = input_

            _LOG.debug("Active source: %s", self._active_source)

            self._play_info = await self._receiver.get_play_info()

            self.update_state()

            self._available = True
            self.events.emit(Events.CONNECTED, self.id)
            self._notify_updated_data()

        except SongpalException as ex:
            _LOG.error("Unable to update: %s", ex)
            self._available = False
        finally:
            self._connecting = False
            self._connect_lock.release()

    async def close_connections(self):
        """Close connections from AVR."""
        _LOG.debug("Close connections %s", self.id)
        self._reconnect_delay = MIN_RECONNECT_DELAY
        if self._connecting:
            return
        self._powered = False
        await self._receiver.stop_listen_notifications()
        if self._websocket_task:
            try:
                self._websocket_task.cancel()
            except CancelledError:
                pass
            finally:
                self._websocket_task = None

    async def disconnect(self):
        """Disconnect from AVR."""
        _LOG.debug("Disconnect %s", self.id)
        await self.close_connections()
        self._available = False
        if self.id:
            self.events.emit(Events.DISCONNECTED, self.id)

    def _notify_updated_data(self):
        """Notify listeners that the AVR data has been updated."""
        # adjust to the real volume level
        # self._expected_volume = self.volume_level

        # None update object means data are up to date & client can fetch required data.
        self.events.emit(Events.UPDATE, self.id, None)

    @property
    def unique_id(self) -> str:
        """Return the unique ID of the device (serial number or mac address if none)."""
        return self._unique_id

    @property
    def attributes(self) -> dict[str, any]:
        """Return the device attributes."""
        updated_data = {
            MediaAttr.STATE: self.state,
            MediaAttr.MUTED: self.is_volume_muted,
            MediaAttr.VOLUME: self.volume_level,
            MediaAttr.SOURCE_LIST: self.source_list,
            MediaAttr.SOURCE: self.source,
            MediaAttr.SOUND_MODE_LIST: self.sound_mode_list,
            MediaAttr.SOUND_MODE: self.sound_mode,
            MediaAttr.MEDIA_IMAGE_URL: self.media_image_url,
            MediaAttr.MEDIA_TITLE: self.media_title,
            MediaAttr.MEDIA_ARTIST: self.media_artist,
            MediaAttr.MEDIA_ALBUM: self.media_album_name
        }
        return updated_data

    @property
    def available(self) -> bool:
        """Return True if device is available."""
        return self._available

    @property
    def name(self) -> str | None:
        """Return the name of the device as string."""
        if self._interface_info:
            return self._interface_info.modelName
        return None

    @property
    def host(self) -> str:
        """Return the host of the device as string."""
        return self._receiver.endpoint

    @property
    def receiver(self) -> Device:
        """Return the receiver device instance."""
        return self._receiver

    @property
    def manufacturer(self) -> str | None:
        """Return the manufacturer of the device as string."""
        if self._interface_info:
            return self._interface_info.productName
        return None

    @property
    def model_name(self) -> str | None:
        """Return the model name of the device as string."""
        if self._interface_info:
            return self._interface_info.modelName
        return None

    @property
    def serial_number(self) -> str | None:
        """Return the serial number of the device as string."""
        if self._sysinfo:
            return self._sysinfo.serialNumber
        return None

    @property
    def support_sound_mode(self) -> bool | None:
        """Return True if sound mode supported."""
        return True
        # return self._receiver.get_soundfield()

    def update_state(self) -> bool:
        """Update device state."""
        old_state = self._state
        if not self._powered:
            self._state = States.OFF
        elif self._playback_state and self._playback_state != States.UNKNOWN:
            self._state = self._playback_state
        else:
            self._state = States.ON
        if old_state != self._state:
            return True
        return False

    @property
    def state(self) -> States:
        """Return the cached state of the device."""
        return self._state

    @property
    def source_list(self) -> list[str]:
        """Return a list of available input sources."""
        return [src.title for src in self._sources.values()]

    @property
    def source(self) -> str:
        """Return the current input source."""
        return getattr(self._active_source, "title", None)

    @property
    def is_volume_muted(self) -> bool:
        """Return boolean if volume is currently muted."""
        return self._attr_is_volume_muted

    @property
    def volume_level(self) -> float | None:
        """Volume level of the media player (0..100)."""
        return 100 * abs((self._volume - self._volume_min) / (self._volume_max - self._volume_min))

    @property
    def sound_mode_list(self) -> list[str]:
        """Return the available sound modes."""
        if self._sound_fields is None:
            return []
        sound_fields: list[str] = []
        for opt in self._sound_fields.candidate:
            sound_fields.append(opt.title)
        return sound_fields

    @property
    def sound_mode(self) -> str:
        """Return the current matched sound mode."""
        if self._sound_fields is None:
            return ""
        return self._sound_fields.currentValue

    @property
    def media_image_url(self) -> str:
        """Image url of current playing media."""
        try:
            return self.get_current_play_info().content.thumbnailUrl
        # pylint: disable = W0718
        except Exception:
            pass
        return ""

    @property
    def media_title(self) -> str:
        """Title of current playing media."""
        try:
            return self.get_current_play_info().title
        # pylint: disable = W0718
        except Exception:
            pass
        return ""

    @property
    def media_artist(self) -> str:
        """Artist of current playing media, music track only."""
        try:
            return self.get_current_play_info().artist
        # pylint: disable = W0718
        except Exception:
            pass
        return ""

    @property
    def media_album_name(self) -> str:
        """Album name of current playing media, music track only."""
        try:
            return self.get_current_play_info().albumName
        # pylint: disable = W0718
        except Exception:
            pass
        return ""

    def get_current_play_info(self) -> PlayInfo | None:
        """Get current playback information."""
        try:
            for play_info in self._play_info:
                if play_info.state and play_info.state != "STOPPED":
                    return play_info
        # pylint: disable = W0718
        except Exception:
            pass
        return None

    @cmd_wrapper
    async def power_on(self):
        """Send power-on command to AVR."""
        await self._receiver.set_power(True)

    @cmd_wrapper
    async def power_off(self):
        """Send power-off command to AVR."""
        try:
            await self._receiver.set_power(False)
        except SongpalException as ex:
            if ex.code == 40000:
                _LOG.debug("Device is probably already off")
                self._state = States.OFF
            else:
                raise ex

    @cmd_wrapper
    async def set_volume_level(self, volume: float | None):
        """Set volume level, range 0..100."""
        if volume is None:
            return ucapi.StatusCodes.BAD_REQUEST
        volume_sony = volume * (self._volume_max - self._volume_min) / 100 + self._volume_min
        _LOG.debug("Sony AVR setting volume to %s", volume_sony)
        await self._volume_control.set_volume(int(volume_sony))

    @cmd_wrapper
    async def volume_up(self):
        """Send volume-up command to AVR."""
        volume_sony = self._volume + VOLUME_STEP * (self._volume_max - self._volume_min) / 100
        volume_sony = min(volume_sony, self._volume_max)
        await self._volume_control.set_volume(int(volume_sony))

    @cmd_wrapper
    async def volume_down(self):
        """Send volume-down command to AVR."""
        volume_sony = self._volume - VOLUME_STEP * (self._volume_max - self._volume_min) / 100
        volume_sony = max(volume_sony, self._volume_min)
        await self._volume_control.set_volume(int(volume_sony))

    @cmd_wrapper
    async def mute(self, muted: bool):
        """Send mute command to AVR."""
        _LOG.debug("Sending mute: %s", muted)
        await self._volume_control.set_mute(muted)
        self.events.emit(Events.UPDATE, self.id, {MediaAttr.MUTED: muted})

    @cmd_wrapper
    async def play_pause(self):
        """Send toggle-play-pause command to AVR."""
        await self._receiver.services["avContent"]["pausePlayingContent"]({})

    @cmd_wrapper
    async def stop(self):
        """Send toggle-play-pause command to AVR."""
        await self._receiver.services["avContent"]["stopPlayingContent"]({})

    @cmd_wrapper
    async def next(self):
        """Send next-track command to AVR."""
        await self._receiver.services["avContent"]["setPlayNextContent"]({})

    @cmd_wrapper
    async def previous(self):
        """Send previous-track command to AVR."""
        await self._receiver.services["avContent"]["setPlayPreviousContent"]({})

    @cmd_wrapper
    async def select_source(self, source: str | None):
        """Send input_source command to AVR."""
        if not source:
            return ucapi.StatusCodes.BAD_REQUEST
        _LOG.debug("Sony AVR set input: %s", source)
        # switch to work.
        await self._receiver.set_power(True)
        for out in self._sources.values():
            if out.title == source:
                await out.activate()
                return ucapi.StatusCodes.OK
        _LOG.error("Sony AVR unable to find output: %s", source)

    @cmd_wrapper
    async def select_sound_mode(self, sound_mode: str | None):
        """Select sound mode."""
        if self._sound_fields is None:
            return ucapi.StatusCodes.BAD_REQUEST
        for opt in self._sound_fields.candidate:
            if opt.title == sound_mode:
                await self._receiver.set_sound_settings("soundField", opt.value)
                break

    @cmd_wrapper
    async def set_sound_settings(self, setting: str, value: any):
        """Select sound mode."""
        if setting is None or value is None:
            return ucapi.StatusCodes.BAD_REQUEST
        await self._receiver.set_sound_settings(setting, value)
