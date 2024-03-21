"""
This module implements the AVR AVR receiver communication of the Remote Two integration driver.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from asyncio import AbstractEventLoop
from collections import OrderedDict
from enum import IntEnum

from songpal import Device, VolumeChange, ContentChange, PowerChange, ConnectChange, SongpalException
from songpal.containers import InterfaceInfo, Sysinfo, Power, StateInfo, Setting, PlayInfo

import ucapi
from config import AvrDevice
from pyee import AsyncIOEventEmitter
from ucapi.media_player import Attributes as MediaAttr

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5
VOLUME_STEP = 2

BACKOFF_MAX: float = 30
MIN_RECONNECT_DELAY: float = 0.5
BACKOFF_FACTOR: float = 1.5

DISCOVERY_AFTER_CONNECTION_ERRORS = 10


class Events(IntEnum):
    """Internal driver events."""

    CONNECTING = 0
    CONNECTED = 1
    DISCONNECTED = 2
    ERROR = 3
    UPDATE = 4
    # IP_ADDRESS_CHANGED = 6


class States(IntEnum):
    """State of a connected AVR."""

    UNKNOWN = 0
    UNAVAILABLE = 1
    OFF = 2
    ON = 3
    PLAYING = 4
    PAUSED = 5
    STOPPED = 6


SONY_PLAYBACK_STATE_MAPPING = {
    "STOPPED": States.STOPPED,
    "PLAYING": States.PLAYING,
    "PAUSED": States.PAUSED,
}


class SonyDevice:
    """Representing a Sony AVR Device."""

    def __init__(
        self,
        device: AvrDevice,
        timeout: float = DEFAULT_TIMEOUT,
        loop: AbstractEventLoop | None = None,
    ):
        """Create instance with given IP or hostname of AVR."""
        # identifier from configuration
        self.id: str = device.id
        # friendly name from configuration
        self._name: str = device.name
        self.event_loop = loop or asyncio.get_running_loop()
        self.events = AsyncIOEventEmitter(self.event_loop)
        self._receiver: Device = Device(device.address)
        self._active: bool = False
        self._attr_available: bool = True

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

        _LOG.debug("Sony AVR created: %s", device.address)

    async def async_activate_websocket(self):
        """Activate websocket for listening if wanted."""
        _LOG.info("Sony AVR Activating websocket connection")

        async def _volume_changed(volume: VolumeChange):
            _LOG.debug("Sony AVR volume changed: %s", volume)
            self._volume = volume.volume
            self._attr_is_volume_muted = volume.mute
            self.events.emit(Events.UPDATE, self.id, {MediaAttr.VOLUME: self.volume_level})

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

        async def _power_changed(power: PowerChange):
            _LOG.debug("Sony AVR Power changed: %s", power)
            self._powered = power.status
            if self.update_state():
                self.events.emit(Events.UPDATE, self.id, {MediaAttr.STATE: self._state})

        async def _try_reconnect(connect: ConnectChange):
            _LOG.warning(
                "Sony AVR  [%s(%s)] Got disconnected, trying to reconnect",
                self._name,
                self._receiver.endpoint,
            )
            _LOG.debug("Disconnected: %s", connect.exception)
            self._attr_available = False
            self._state = States.UNKNOWN
            self._notify_updated_data()

            # Try to reconnect forever, a successful reconnect will initialize
            # the websocket connection again.
            delay = DISCOVERY_AFTER_CONNECTION_ERRORS
            while not self._attr_available:
                _LOG.debug("Sony AVR Trying to reconnect in %s seconds", delay)
                self.events.emit(Events.CONNECTING, self.id)
                await asyncio.sleep(delay)

                try:
                    await self._receiver.get_supported_methods()
                except SongpalException as ex:
                    _LOG.debug("Sony AVR Failed to reconnect: %s", ex)
                    delay = min(2 * delay, 300)
                else:
                    # We need to inform Remote about the state in case we are coming
                    # back from a disconnected state and update internal data
                    await self.connect()

                    # self._notify_updated_data()
            await self.event_loop.create_task(self._receiver.listen_notifications())
            _LOG.warning("Sony AVR [%s(%s)] Connection reestablished", self._name, self._receiver.endpoint)

        self._receiver.on_notification(VolumeChange, _volume_changed)
        self._receiver.on_notification(ContentChange, _source_changed)
        self._receiver.on_notification(PowerChange, _power_changed)
        self._receiver.on_notification(ConnectChange, _try_reconnect)

        # Start websocket
        await self.event_loop.create_task(self._receiver.listen_notifications())

    async def connect(self):
        try:
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
                self._attr_available = False
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

            self._attr_available = True
            self.events.emit(Events.CONNECTED, self.id)
            self._notify_updated_data()

        except SongpalException as ex:
            _LOG.error("Unable to update: %s", ex)
            self._attr_available = False
        finally:
            self._connecting = False

    async def disconnect(self):
        """Disconnect from AVR."""
        _LOG.debug("Disconnect %s", self.id)
        self._reconnect_delay = MIN_RECONNECT_DELAY
        # Note: disconnecting during a connection task is currently not supported!
        # Simply setting self._connecting = False doesn't work, and will start even more connection tasks after wakeup!
        # This requires a state machine, or at least a separate connection task which can be cancelled.
        if self._connecting:
            return
        self._powered = False
        await self._receiver.stop_listen_notifications()

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
    def active(self) -> bool:
        """Return true if device is active and should have an established connection."""
        return self._active

    @property
    def available(self) -> bool:
        """Return True if device is available."""
        return self._attr_available

    @available.setter
    def available(self, value: bool):
        """Set device availability and emit CONNECTED / DISCONNECTED event on change."""
        if self._attr_available != value:
            self._attr_available = value
            self.events.emit(Events.CONNECTED if value else Events.DISCONNECTED, self.id)

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
        old_state = self._state
        if not self._state:
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
        except Exception:
            pass
        return ""

    @property
    def media_title(self) -> str:
        """Title of current playing media."""
        try:
            return self.get_current_play_info().title
        except Exception:
            pass
        return ""

    @property
    def media_artist(self) -> str:
        """Artist of current playing media, music track only."""
        try:
            return self.get_current_play_info().artist
        except Exception:
            pass
        return ""

    @property
    def media_album_name(self) -> str:
        """Album name of current playing media, music track only."""
        try:
            return self.get_current_play_info().albumName
        except Exception:
            pass
        return ""

    def get_current_play_info(self) -> PlayInfo | None:
        try:
            for play_info in self._play_info:
                if play_info.state and play_info.state != "STOPPED":
                    return play_info
        except Exception:
            pass
        return None

    async def power_on(self) -> ucapi.StatusCodes:
        """Send power-on command to AVR."""
        try:
            await self._receiver.set_power(True)
            return ucapi.StatusCodes.OK
        except SongpalException as ex:
            _LOG.error("Sony AVR error power_on", ex)
            return ucapi.StatusCodes.BAD_REQUEST

    async def power_off(self) -> ucapi.StatusCodes:
        """Send power-off command to AVR."""
        try:
            await self._receiver.set_power(False)
            return ucapi.StatusCodes.OK
        except SongpalException as ex:
            _LOG.error("Sony AVR error power_on", ex)
            return ucapi.StatusCodes.BAD_REQUEST

    async def set_volume_level(self, volume: float | None) -> ucapi.StatusCodes:
        """Set volume level, range 0..100."""
        if volume is None:
            return ucapi.StatusCodes.BAD_REQUEST
        volume_sony = volume * (self._volume_max - self._volume_min) / 100 + self._volume_min
        _LOG.debug("Sony AVR setting volume to %s", volume_sony)
        await self._volume_control.set_volume(int(volume_sony))
        self.events.emit(Events.UPDATE, self.id, {MediaAttr.VOLUME: volume})
        return ucapi.StatusCodes.OK

    async def volume_up(self) -> ucapi.StatusCodes:
        """Send volume-up command to AVR."""
        volume_sony = self._volume + VOLUME_STEP * (self._volume_max - self._volume_min) / 100
        volume_sony = min(volume_sony, self._volume_max)
        try:
            await self._volume_control.set_volume(int(volume_sony))
        except SongpalException as ex:
            _LOG.error("Sony AVR error volume_up", ex)
            return ucapi.StatusCodes.BAD_REQUEST
        return ucapi.StatusCodes.OK

    async def volume_down(self) -> ucapi.StatusCodes:
        """Send volume-down command to AVR."""
        volume_sony = self._volume - VOLUME_STEP * (self._volume_max - self._volume_min) / 100
        volume_sony = max(volume_sony, self._volume_min)
        try:
            await self._volume_control.set_volume(int(volume_sony))
        except SongpalException as ex:
            _LOG.error("Sony AVR error volume_down", ex)
            return ucapi.StatusCodes.BAD_REQUEST
        return ucapi.StatusCodes.OK

    async def mute(self, muted: bool) -> ucapi.StatusCodes:
        """Send mute command to AVR."""
        _LOG.debug("Sending mute: %s", muted)
        try:
            await self._volume_control.set_mute(muted)
            self.events.emit(Events.UPDATE, self.id, {MediaAttr.MUTED: muted})
        except SongpalException as ex:
            _LOG.error("Sony AVR error mute", ex)
            return ucapi.StatusCodes.BAD_REQUEST
        return ucapi.StatusCodes.OK

    async def play_pause(self) -> ucapi.StatusCodes:
        """Send toggle-play-pause command to AVR."""
        try:
            await self._receiver.services["avContent"]["pausePlayingContent"]({})
        except SongpalException as ex:
            _LOG.error("Sony AVR error play_pause", ex)
            return ucapi.StatusCodes.BAD_REQUEST

        return ucapi.StatusCodes.OK

    async def stop(self) -> ucapi.StatusCodes:
        """Send toggle-play-pause command to AVR."""
        try:
            await self._receiver.services["avContent"]["stopPlayingContent"]({})
        except SongpalException as ex:
            _LOG.error("Sony AVR error stop", ex)
            return ucapi.StatusCodes.BAD_REQUEST
        return ucapi.StatusCodes.OK

    async def next(self) -> ucapi.StatusCodes:
        """Send next-track command to AVR."""
        try:
            await self._receiver.services["avContent"]["setPlayNextContent"]({})
        except SongpalException as ex:
            _LOG.error("Sony AVR error next", ex)
            return ucapi.StatusCodes.BAD_REQUEST
        return ucapi.StatusCodes.OK

    async def previous(self) -> ucapi.StatusCodes:
        """Send previous-track command to AVR."""
        try:
            await self._receiver.services["avContent"]["setPlayPreviousContent"]({})
        except SongpalException as ex:
            _LOG.error("Sony AVR error previous", ex)
            return ucapi.StatusCodes.BAD_REQUEST
        return ucapi.StatusCodes.OK

    async def select_source(self, source: str | None) -> ucapi.StatusCodes:
        """Send input_source command to AVR."""
        if not source:
            return ucapi.StatusCodes.BAD_REQUEST
        _LOG.debug("Sony AVR set input: %s", source)
        # switch to work.
        try:
            await self.power_on()
            for out in self._sources.values():
                if out.title == source:
                    await out.activate()
                    return ucapi.StatusCodes.OK
            _LOG.error("Sony AVR unable to find output: %s", source)
            return ucapi.StatusCodes.BAD_REQUEST
        except SongpalException as ex:
            _LOG.error("Sony AVR error select_source", ex)
            return ucapi.StatusCodes.BAD_REQUEST

    async def select_sound_mode(self, sound_mode: str | None) -> ucapi.StatusCodes:
        """Select sound mode."""
        if self._sound_fields is None:
            return ucapi.StatusCodes.BAD_REQUEST
        try:
            for opt in self._sound_fields.candidate:
                if opt.title == sound_mode:
                    await self._receiver.set_sound_settings("soundField", opt.value)
                    break
            return ucapi.StatusCodes.OK
        except SongpalException as ex:
            _LOG.error("Sony AVR error select_sound_mode", ex)
            return ucapi.StatusCodes.BAD_REQUEST
