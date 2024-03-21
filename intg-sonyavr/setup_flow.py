"""
Setup flow for Sony AVR integration.

:copyright: (c) 2023 by Unfolded Circle ApS.
:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import asyncio
import logging
from enum import IntEnum

from songpal import Device, SongpalException
import config
import discover
from config import AvrDevice
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

_LOG = logging.getLogger(__name__)


class SetupSteps(IntEnum):
    """Enumeration of setup steps to keep track of user data responses."""

    INIT = 0
    CONFIGURATION_MODE = 1
    DISCOVER = 2
    DEVICE_CHOICE = 3


_setup_step = SetupSteps.INIT
_cfg_add_device: bool = False
_user_input_discovery = RequestUserInput(
    {"en": "Setup mode", "de": "Setup Modus"},
    [
        {"field": {"text": {"value": ""}}, "id": "address", "label": {"en": "Endpoint", "de": "IP-Adresse", "fr": "Adresse"}},
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
        _LOG.error("No or invalid user response was received: %s", msg)
    elif isinstance(msg, AbortDriverSetup):
        _LOG.info("Setup was aborted with code: %s", msg.error)
        _setup_step = SetupSteps.INIT

    # user confirmation not used in setup process
    # if isinstance(msg, UserConfirmationResponse):
    #     return handle_user_confirmation(msg)

    return SetupError()


async def handle_driver_setup(_msg: DriverSetupRequest) -> RequestUserInput | SetupError:
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
                    "field": {"dropdown": {"value": dropdown_devices[0]["id"], "items": dropdown_devices}},
                    "id": "choice",
                    "label": {
                        "en": "Configured devices",
                        "de": "Konfigurierte Geräte",
                        "fr": "Appareils configurés",
                    },
                },
                {
                    "field": {"dropdown": {"value": dropdown_actions[0]["id"], "items": dropdown_actions}},
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


async def handle_configuration_mode(msg: UserDataResponse) -> RequestUserInput | SetupComplete | SetupError:
    """
    Process user data response in a setup process.

    If ``address`` field is set by the user: try connecting to device and retrieve model information.
    Otherwise, start Android TV discovery and present the found devices to the user to choose from.

    :param msg: response data from the requested user data
    :return: the setup action on how to continue
    """
    global _setup_step
    global _cfg_add_device

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

    config.devices.clear()  # triggers device instance removal

    dropdown_items = []
    address = msg.input_values["address"]

    if address:
        _LOG.debug("Starting manual driver setup for %s", address)
        try:
            # simple connection check
            device = Device(address)
            await device.get_supported_methods()
            interface_info = await device.get_interface_information()
            dropdown_items.append({"id": address, "label": {"en": f"{interface_info.modelName} [{address}]"}})
        except SongpalException as ex:
            _LOG.error("Cannot connect to manually entered address %s: %s", address, ex)
            return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)
    else:
        _LOG.debug("Starting auto-discovery driver setup")
        avrs = await discover.sony_avrs()
        for a in avrs:
            avr_data = {"id": a.endpoint, "label": {"en": f"{a.name} ({a.model_number}) [{a.endpoint}]"}}
            dropdown_items.append(avr_data)

    if not dropdown_items:
        _LOG.warning("No AVRs found")
        return SetupError(error_type=IntegrationSetupError.NOT_FOUND)

    _setup_step = SetupSteps.DEVICE_CHOICE
    return RequestUserInput(
        {"en": "Please choose your Sony AVR", "de": "Bitte Sony AVR auswählen", "fr": "Sélectionnez votre ampli Sony"},
        [
            {
                "field": {"dropdown": {"value": dropdown_items[0]["id"], "items": dropdown_items}},
                "id": "choice",
                "label": {
                    "en": "Choose your Sony AVR",
                    "de": "Wähle deinen Sony AVR",
                    "fr": "Choisissez votre Sony AVR",
                },
            }
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
    _LOG.debug("Chosen Sony AVR: %s. Trying to connect and retrieve device information...", host)
    try:
        # simple connection check
        device = Device(host)
        await device.get_supported_methods()
        interface_info = await device.get_interface_information()
        system_info = await device.get_system_info()
    except SongpalException as ex:
        _LOG.error("Cannot connect to %s: %s", host, ex)
        return SetupError(error_type=IntegrationSetupError.CONNECTION_REFUSED)

    assert device
    assert system_info

    unique_id = system_info.serialNumber
    if unique_id is None:
        unique_id = system_info.macAddr
    if unique_id is None:
        unique_id = system_info.wirelessMacAddr

    if unique_id is None:
        _LOG.error("Could not get mac address of host %s: required to create a unique device", host)
        return SetupError(error_type=IntegrationSetupError.OTHER)

    config.devices.add(
        AvrDevice(id=unique_id, name=interface_info.modelName, address=host)
    )  # triggers SonyAVR instance creation
    config.devices.store()

    # AVR device connection will be triggered with subscribe_entities request

    await asyncio.sleep(1)

    _LOG.info("Setup successfully completed for %s (%s)", interface_info.modelName, unique_id)
    return SetupComplete()
