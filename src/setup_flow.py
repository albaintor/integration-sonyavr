"""
Setup flow for Sony AVR integration.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from enum import IntEnum
from urllib.parse import urlparse

import config
import discover
from config import AvrDevice
from songpal import Device, SongpalException
from ucapi import (
    AbortDriverSetup,
    DriverSetupRequest,
    IntegrationSetupError,
    RequestUserInput,
    SetupAction,
    SetupComplete,
    SetupDriver,
    SetupError,
    UserDataResponse,
)

from const import DEFAULT_PORT

_LOG = logging.getLogger(__name__)


class SetupSteps(IntEnum):
    """Enumeration of setup steps to keep track of user data responses."""

    INIT = 0
    CONFIGURATION_MODE = 1
    DISCOVER = 2
    DEVICE_CHOICE = 3
    RECONFIGURE = 4


_setup_step = SetupSteps.INIT
_cfg_add_device: bool = False
_reconfigured_device: AvrDevice | None = None
# pylint: disable = C0301
# flake8: noqa

_user_input_discovery = RequestUserInput(
    {"en": "Setup mode", "de": "Setup Modus"},
    [
        {
            "field": {"text": {"value": ""}},
            "id": "address",
            "label": {"en": "Endpoint", "de": "IP-Adresse", "fr": "Adresse"},
        },
        {
            "id": "info",
            "label": {"en": ""},
            "field": {
                "label": {
                    "value": {
                        "en": "Leave blank to use auto-discovery. Otherwise expected format : http://<ip address>:10000/sony",
                        "de": "Leer lassen, um automatische Erkennung zu verwenden. Ansonsten ist das erwartete format : http://<ip address>:10000/sony",
                        "fr": "Laissez le champ vide pour utiliser la découverte automatique. Sinon format attendu : http://<ip address>:10000/sony",
                    }
                }
            },
        },
    ],
)


async def driver_setup_handler(msg: SetupDriver) -> SetupAction:
    """
    Dispatch driver setup requests to corresponding handlers.

    Either start the setup process or handle the selected AVR device.

    :param msg: the setup driver request object, either DriverSetupRequest or UserDataResponse
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device

    if isinstance(msg, DriverSetupRequest):
        _setup_step = SetupSteps.INIT
        _cfg_add_device = False
        return await handle_driver_setup(msg)
    if isinstance(msg, UserDataResponse):
        _LOG.debug(msg)
        if _setup_step == SetupSteps.CONFIGURATION_MODE and "action" in msg.input_values:
            return await handle_configuration_mode(msg)
        if _setup_step == SetupSteps.DISCOVER and "address" in msg.input_values:
            return await _handle_discovery(msg)
        if _setup_step == SetupSteps.DEVICE_CHOICE and "choice" in msg.input_values:
            return await handle_device_choice(msg)
        if _setup_step == SetupSteps.RECONFIGURE:
            return await _handle_device_reconfigure(msg)
        _LOG.error("No or invalid user response was received: %s", msg)
    elif isinstance(msg, AbortDriverSetup):
        _LOG.info("Setup was aborted with code: %s", msg.error)
        _setup_step = SetupSteps.INIT

    # user confirmation not used in setup process
    # if isinstance(msg, UserConfirmationResponse):
    #     return handle_user_confirmation(msg)

    return SetupError()


async def handle_driver_setup(
        _msg: DriverSetupRequest,
) -> RequestUserInput | SetupError:
    """
    Start driver setup.

    Initiated by Remote Two to set up the driver.
    Ask user to enter ip-address for manual configuration, otherwise auto-discovery is used.

    :param _msg: not used, we don't have any input fields in the first setup screen.
    :return: the setup action on how to continue
    """
    global _setup_step

    reconfigure = _msg.reconfigure
    _LOG.debug("Starting driver setup, reconfigure=%s", reconfigure)

    # workaround for web-configurator not picking up first response
    await asyncio.sleep(1)

    if reconfigure:
        _setup_step = SetupSteps.CONFIGURATION_MODE

        # get all configured devices for the user to choose from
        dropdown_devices = []
        for device in config.devices.all():
            dropdown_devices.append({"id": device.id, "label": {"en": f"{device.name} ({device.id})"}})

        # TODO #12 externalize language texts
        # build user actions, based on available devices
        dropdown_actions = [
            {
                "id": "add",
                "label": {
                    "en": "Add a new device",
                    "de": "Neues Gerät hinzufügen",
                    "fr": "Ajouter un nouvel appareil",
                },
            },
        ]

        # add remove & reset actions if there's at least one configured device
        if dropdown_devices:
            dropdown_actions.append(
                {
                    "id": "configure",
                    "label": {
                        "en": "Configure selected device",
                        "fr": "Configurer l'appareil sélectionné",
                    },
                },
            )
            dropdown_actions.append(
                {
                    "id": "remove",
                    "label": {
                        "en": "Delete selected device",
                        "de": "Selektiertes Gerät löschen",
                        "fr": "Supprimer l'appareil sélectionné",
                    },
                },
            )
            dropdown_actions.append(
                {
                    "id": "reset",
                    "label": {
                        "en": "Reset configuration and reconfigure",
                        "de": "Konfiguration zurücksetzen und neu konfigurieren",
                        "fr": "Réinitialiser la configuration et reconfigurer",
                    },
                },
            )
        else:
            # dummy entry if no devices are available
            dropdown_devices.append({"id": "", "label": {"en": "---"}})

        return RequestUserInput(
            {"en": "Configuration mode", "de": "Konfigurations-Modus"},
            [
                {
                    "field": {
                        "dropdown": {
                            "value": dropdown_devices[0]["id"],
                            "items": dropdown_devices,
                        }
                    },
                    "id": "choice",
                    "label": {
                        "en": "Configured devices",
                        "de": "Konfigurierte Geräte",
                        "fr": "Appareils configurés",
                    },
                },
                {
                    "field": {
                        "dropdown": {
                            "value": dropdown_actions[0]["id"],
                            "items": dropdown_actions,
                        }
                    },
                    "id": "action",
                    "label": {
                        "en": "Action",
                        "de": "Aktion",
                        "fr": "Appareils configurés",
                    },
                },
            ],
        )

    # Initial setup, make sure we have a clean configuration
    config.devices.clear()  # triggers device instance removal
    _setup_step = SetupSteps.DISCOVER
    return _user_input_discovery


async def handle_configuration_mode(
        msg: UserDataResponse,
) -> RequestUserInput | SetupComplete | SetupError:
    """
    Process user data response in a setup process.

    If ``address`` field is set by the user: try connecting to device and retrieve model information.
    Otherwise, start Android TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device
    global _reconfigured_device

    action = msg.input_values["action"]

    # workaround for web-configurator not picking up first response
    await asyncio.sleep(1)

    match action:
        case "add":
            _cfg_add_device = True
        case "remove":
            choice = msg.input_values["choice"]
            if not config.devices.remove(choice):
                _LOG.warning("Could not remove device from configuration: %s", choice)
                return SetupError(error_type=IntegrationSetupError.OTHER)
            config.devices.store()
            return SetupComplete()
        case "reset":
            config.devices.clear()  # triggers device instance removal
        case "configure":
            # Reconfigure device if the identifier has changed
            choice = msg.input_values["choice"]
            selected_device = config.devices.get(choice)
            if not selected_device:
                _LOG.warning("Can not configure device from configuration: %s", choice)
                return SetupError(error_type=IntegrationSetupError.OTHER)

            _setup_step = SetupSteps.RECONFIGURE
            _reconfigured_device = selected_device

            return RequestUserInput(
                {
                    "en": "Configure your Sony AVR",
                    "fr": "Configurez votre ampli Sony",
                },
                [
                    {
                        "field": {"text": {"value": _reconfigured_device.address}},
                        "id": "address",
                        "label": {"en": "IP address", "de": "IP-Adresse", "fr": "Adresse IP"},
                    },
                    {
                        "id": "always_on",
                        "label": {
                            "en": "Keep connection alive (faster initialization, but consumes more battery)",
                            "fr": "Conserver la connexion active (lancement plus rapide, mais consomme plus de batterie)",
                        },
                        "field": {"checkbox": {"value": _reconfigured_device.always_on}},
                    },
                    {
                        "id": "volume_step",
                        "label": {
                            "en": "Volume step",
                            "fr": "Pallier de volume",
                        },
                        "field": {
                            "number": {"value": _reconfigured_device.volume_step, "min": 1.0, "max": 10, "steps": 1, "decimals": 1,
                                       "unit": {"en": "dB"}}
                        },
                    }
                ],
            )
        case _:
            _LOG.error("Invalid configuration action: %s", action)
            return SetupError(error_type=IntegrationSetupError.OTHER)

    _setup_step = SetupSteps.DISCOVER
    return _user_input_discovery


async def _handle_discovery(msg: UserDataResponse) -> RequestUserInput | SetupError:
    """
    Process user data response in a setup process.

    If ``address`` field is set by the user: try connecting to device and retrieve model information.
    Otherwise, start AVR discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step

    dropdown_items = []
    address = msg.input_values["address"]

    if address:
        _LOG.debug("Starting manual driver setup for %s", address)
        try:
            if not address.startswith("http://"):
                address = f"http://{address}"

            result = urlparse(address)
            path = result.path
            port = result.port
            if not path:
                path = "/sony"
            if not port:
                port = DEFAULT_PORT
            address = f"{result.scheme}://{result.hostname}:{port}{path}"

            _LOG.debug("Formatted address : %s", address)
            # simple connection check
            device = Device(address)
            await device.get_supported_methods()
            interface_info = await device.get_interface_information()
            dropdown_items.append(
                {
                    "id": address,
                    "label": {"en": f"{interface_info.modelName} [{address}]"},
                }
            )
        except SongpalException as ex:
            _LOG.error("Cannot connect to manually entered address %s: %s", address, ex)
            return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)
    else:
        _LOG.debug("Starting auto-discovery driver setup")
        avrs = await discover.sony_avrs()
        for a in avrs:
            avr_data = {
                "id": a.endpoint,
                "label": {"en": f"{a.name} ({a.model_number}) [{a.endpoint}]"},
            }
            dropdown_items.append(avr_data)

    if not dropdown_items:
        _LOG.warning("No AVRs found")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _setup_step = SetupSteps.DEVICE_CHOICE
    return RequestUserInput(
        {
            "en": "Please choose your Sony AVR",
            "de": "Bitte Sony AVR auswählen",
            "fr": "Sélectionnez votre ampli Sony",
        },
        [
            {
                "field": {
                    "dropdown": {
                        "value": dropdown_items[0]["id"],
                        "items": dropdown_items,
                    }
                },
                "id": "choice",
                "label": {
                    "en": "Choose your Sony AVR",
                    "de": "Wähle deinen Sony AVR",
                    "fr": "Choisissez votre Sony AVR",
                },
            },
            {
                "id": "always_on",
                "label": {
                    "en": "Keep connection alive (faster initialization, but consumes more battery)",
                    "fr": "Conserver la connexion active (lancement plus rapide, mais consomme plus de batterie)",
                },
                "field": {"checkbox": {"value": False}},
            },
            {
                "id": "volume_step",
                "label": {
                    "en": "Volume step",
                    "fr": "Pallier de volume",
                },
                "field": {
                    "number": {"value": 2.0, "min": 1.0, "max": 10, "steps": 1, "decimals": 1, "unit": {"en": "dB"}}
                },
            },
        ],
    )


async def handle_device_choice(msg: UserDataResponse) -> SetupComplete | SetupError:
    """
    Process user data response in a setup process.

    Driver setup callback to provide requested user data during the setup process.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue: SetupComplete if a valid AVR device was chosen.
    """
    host = msg.input_values["choice"]
    always_on = msg.input_values.get("always_on") == "true"
    try:
        volume_step = float(msg.input_values.get("volume_step", 1.0))
        if volume_step < 0.1 or volume_step > 10:
            return SetupError(error_type=IntegrationSetupError.OTHER)
    except ValueError:
        return SetupError(error_type=IntegrationSetupError.OTHER)
    _LOG.debug(
        "Chosen Sony AVR: %s. Trying to connect and retrieve device information...",
        host,
    )

    try:
        device: AvrDevice = await config.Devices.extract_device_info(host)
    except SongpalException as ex:
        _LOG.error("Cannot connect to %s: %s", host, ex)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)

    if device is None or device.id is None:
        _LOG.error(
            "Could not get mac address of host %s: required to create a unique device",
            host,
        )
        return SetupError(error_type=IntegrationSetupError.OTHER)

    device.always_on = always_on
    device.volume_step = volume_step
    config.devices.store()

    # AVR device connection will be triggered with subscribe_entities request
    config.devices.add_or_update(device)  # triggers ATV instance update
    config.devices.store()

    await asyncio.sleep(1)

    _LOG.info("Setup successfully completed for %s (%s)", device.name, device.id)
    return SetupComplete()


async def _handle_device_reconfigure(msg: UserDataResponse) -> SetupComplete | SetupError:
    """
    Process reconfiguration of a registered Android TV device.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue: SetupComplete after updating configuration
    """
    # flake8: noqa:F824
    # pylint: disable=W0602
    global _reconfigured_device

    if _reconfigured_device is None:
        return SetupError()

    address = msg.input_values.get("address", "")
    try:
        volume_step = float(msg.input_values.get("volume_step", 1.0))
    except ValueError:
        return SetupError(error_type=IntegrationSetupError.OTHER)
    always_on = msg.input_values.get("always_on") == "true"

    _LOG.debug("User has changed configuration")
    _reconfigured_device.address = address
    _reconfigured_device.volume_step = volume_step
    _reconfigured_device.always_on = always_on

    config.devices.add_or_update(_reconfigured_device)  # triggers ATV instance update
    await asyncio.sleep(1)
    _LOG.info("Setup successfully completed for %s", _reconfigured_device.name)

    return SetupComplete()