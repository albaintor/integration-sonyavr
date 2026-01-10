"""Constants file.

:copyright: (c) 2023 by Albaintor
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

from enum import Enum

DEFAULT_PORT = 10000
DEFAULT_VOLUME_STEP = 2.0

SIMPLE_COMMANDS = ["ZONE_HDMI_OUTPUT_AB", "ZONE_HDMI_OUTPUT_A", "ZONE_HDMI_OUTPUT_B", "ZONE_HDMI_OUTPUT_OFF"]


class SonySensors(str, Enum):
    """Sony sensor values."""

    SENSOR_VOLUME = "sensor_volume"
    SENSOR_MUTED = "sensor_muted"
    SENSOR_INPUT = "sensor_input"
    SENSOR_SOUND_MODE = "sensor_sound_mode"
