"""Parser for BLE advertisements in BThome format.

This file is shamelessly copied from the following repository:
https://github.com/Ernst79/bleparser/blob/ac8757ad64f1fc17674dcd22111e547cdf2f205b/package/bleparser/ha_ble.py

BThome was originally developed as HA BLE for ble_monitor and been renamed to
BThome for Home Assistant Bluetooth integrations.

MIT License applies.
"""
from __future__ import annotations

import logging
import struct
import sys
from enum import Enum
from typing import Any

from bluetooth_sensor_state_data import BluetoothData
from Cryptodome.Cipher import AES
from home_assistant_bluetooth import BluetoothServiceInfo

from .const import MEAS_TYPES

_LOGGER = logging.getLogger(__name__)


def short_address(address: str) -> str:
    """Convert a Bluetooth address to a short address"""
    return address.replace("-", "").replace(":", "")[-6:].upper()


class EncryptionScheme(Enum):

    # No encryption is needed to use this device
    NONE = "none"

    # 16 byte encryption key expected
    BTHOME_BINDKEY = "bthome_bindkey"


def to_mac(addr: bytes) -> str:
    """Return formatted MAC address"""
    return ":".join(f"{i:02X}" for i in addr)


def parse_uint(data_obj: bytes, factor: float = 1.0) -> float:
    """convert bytes (as unsigned integer) and factor to float"""
    decimal_places = -int(f"{factor:e}".split("e")[-1])
    return round(
        int.from_bytes(data_obj, "little", signed=False) * factor, decimal_places
    )


def parse_int(data_obj: bytes, factor: float = 1.0) -> float:
    """convert bytes (as signed integer) and factor to float"""
    decimal_places = -int(f"{factor:e}".split("e")[-1])
    return round(
        int.from_bytes(data_obj, "little", signed=True) * factor, decimal_places
    )


def parse_float(data_obj: bytes, factor: float = 1.0) -> float | None:
    """convert bytes (as float) and factor to float"""
    decimal_places = -int(f"{factor:e}".split("e")[-1])
    if len(data_obj) == 2:
        [val] = struct.unpack("e", data_obj)
    elif len(data_obj) == 4:
        [val] = struct.unpack("f", data_obj)
    elif len(data_obj) == 8:
        [val] = struct.unpack("d", data_obj)
    else:
        _LOGGER.error("only 2, 4 or 8 byte long floats are supported in BThome BLE")
        return None
    return round(val * factor, decimal_places)


def parse_string(data_obj: bytes) -> str:
    """convert bytes to string"""
    return data_obj.decode("UTF-8")


def parse_mac(data_obj: bytes) -> bytes | None:
    """convert bytes to mac"""
    if len(data_obj) == 6:
        return data_obj[::-1]
    else:
        _LOGGER.error("MAC address has to be 6 bytes long")
        return None


class BThomeBluetoothDeviceData(BluetoothData):
    """Date for BThome Bluetooth devices."""

    def __init__(self, bindkey: bytes | None = None) -> None:
        super().__init__()
        self.bindkey = bindkey

        # Data that we know how to parse but don't yet map to the SensorData model.
        self.unhandled: dict[str, Any] = {}

        # Encryption to expect, based on flags in the UUID.
        self.encryption_scheme = EncryptionScheme.NONE

        # If true, then we know the actual MAC of the device.
        # On macOS, we don't unless the device includes it in the advertisement
        # (CoreBluetooth uses UUID's generated by CoreBluetooth instead of the MAC)
        self.mac_known = sys.platform != "darwin"

        # If true then we have used the provided encryption key to decrypt at least
        # one payload.
        # If false then we have either not seen an encrypted payload, the key is wrong
        # or encryption is not in use
        self.bindkey_verified = False

        # If this is True, then we have not seen an advertisement with a payload
        # Until we see a payload, we can't tell if this device is encrypted or not
        self.pending = True

        # The last service_info we saw that had a payload
        # We keep this to help in reauth flows where we want to reprocess and old
        # value with a new bindkey.
        self.last_service_info: BluetoothServiceInfo | None = None

    def supported(self, data: BluetoothServiceInfo) -> bool:
        if not super().supported(data):
            return False

        # Where a device uses encryption we need to know its actual MAC address.
        # As the encryption uses it as part of the nonce.
        # On macOS we instead only know its CoreBluetooth UUID.
        # It seems its impossible to automatically get that in the general case.
        # So devices do duplicate the MAC in the advertisement, we use that
        # when we can on macOS.
        # We may want to ask the user for the MAC address during config flow
        # For now, just hide these devices for macOS users.
        if self.encryption_scheme != EncryptionScheme.NONE:
            if not self.mac_known:
                return False

        return True

    def _start_update(self, service_info: BluetoothServiceInfo) -> None:
        """Update from BLE advertisement data."""
        _LOGGER.debug("Parsing BThome BLE advertisement data: %s", service_info)
        self.set_device_manufacturer("Home Assistant")
        self.set_device_type("BThome sensor")
        for uuid, data in service_info.service_data.items():
            if self._parse_bthome(service_info, service_info.name, data):
                self.last_service_info = service_info

    def _parse_bthome(
        self, service_info: BluetoothServiceInfo, name: str, data: bytes
    ) -> bool:
        """Parser for BThome sensors"""
        mac_readable = service_info.address
        if len(mac_readable) != 17 and mac_readable[2] != ":":
            # On macOS, we get a UUID, which is useless for BThome sensors
            self.mac_known = False
            return False
        else:
            self.mac_known = True
        source_mac = bytes.fromhex(mac_readable.replace(":", ""))

        identifier = short_address(service_info.address)
        if name[-6:] == identifier:
            # Remove the identifier if it is already in the local name.
            name = name[:-6]
            if name[-1:] in ("_", " "):
                name = name[:-1]
        self.set_device_name(f"{name} {identifier}")
        self.set_title(f"{name} {identifier}")

        uuid16 = service_info.service_uuids
        if uuid16 == ["0000181c-0000-1000-8000-00805f9b34fb"]:
            # Non-encrypted BThome BLE format
            self.encryption_scheme = EncryptionScheme.NONE
            self.set_device_sw_version("BThome BLE")
            payload = data
            packet_id = None  # noqa: F841
        elif uuid16 == ["0000181e-0000-1000-8000-00805f9b34fb"]:
            # Encrypted BThome BLE format
            self.encryption_scheme = EncryptionScheme.BTHOME_BINDKEY
            self.set_device_sw_version("BThome BLE (encrypted)")
            try:
                payload = self._decrypt_bthome(data, source_mac)
            except (ValueError, TypeError):
                return False

            packet_id = parse_uint(data[-8:-4])  # noqa: F841
        else:
            return False

        payload_length = len(payload)
        payload_start = 0
        result = False

        while payload_length >= payload_start + 1:
            meas_float = None
            meas_str = None

            obj_control_byte = payload[payload_start]
            obj_data_length = (obj_control_byte >> 0) & 31  # 5 bits (0-4)
            obj_data_format = (obj_control_byte >> 5) & 7  # 3 bits (5-7)
            obj_meas_type = payload[payload_start + 1]
            next_start = payload_start + 1 + obj_data_length
            if payload_length < next_start:
                _LOGGER.debug("Invalid payload data length, payload: %s", payload.hex())
                break

            if obj_data_length != 0:
                if obj_data_format <= 3:
                    if obj_meas_type in MEAS_TYPES:
                        next_payload = payload_start + 2
                        meas_data = payload[next_payload:next_start]
                        meas_type = MEAS_TYPES[obj_meas_type]
                        meas_format = meas_type.meas_format
                        meas_factor = meas_type.factor

                        if obj_data_format == 0:
                            meas_float = parse_uint(meas_data, meas_factor)
                        elif obj_data_format == 1:
                            meas_float = parse_int(meas_data, meas_factor)
                        elif obj_data_format == 2:
                            meas_float = parse_float(meas_data, meas_factor)
                        elif obj_data_format == 3:
                            meas_str = parse_string(meas_data)

                        if meas_float:
                            self.update_predefined_sensor(meas_format, meas_float)
                            result = True
                        elif meas_str:
                            _LOGGER.debug(
                                "String data type not supported yet! Adv: %s",
                                data.hex(),
                            )
                        else:
                            _LOGGER.debug(
                                "UNKNOWN dataobject in BThome BLE payload! Adv: %s",
                                data.hex(),
                            )
                    else:
                        _LOGGER.debug(
                            "UNKNOWN measurement type in BThome BLE payload! Adv: %s",
                            data.hex(),
                        )
                elif obj_data_format == 4:
                    # Using a different MAC address than the source mac address
                    # is not supported yet
                    mac_start = payload_start + 1
                    data_mac = parse_mac(payload[mac_start:next_start])
                    if data_mac:
                        bthome_ble_mac = data_mac  # noqa: F841
                else:
                    _LOGGER.error(
                        "UNKNOWN dataobject in BThome BLE payload! Adv: %s",
                        data.hex(),
                    )
            payload_start = next_start

        if not result:
            return False

        return True

    def _decrypt_bthome(self, data: bytes, bthome_mac: bytes) -> bytes:
        """Decrypt encrypted BThome BLE advertisements"""
        if not self.bindkey:
            self.bindkey_verified = False
            _LOGGER.debug("Encryption key not set and adv is encrypted")
            raise ValueError

        if not self.bindkey or len(self.bindkey) != 16:
            self.bindkey_verified = False
            _LOGGER.error("Encryption key should be 16 bytes (32 characters) long")
            raise ValueError

        # check for minimum length of encrypted advertisement
        if len(data) < 15:
            _LOGGER.debug("Invalid data length (for decryption), adv: %s", data.hex())
            raise ValueError

        # prepare the data for decryption
        uuid = b"\x1e\x18"
        encrypted_payload = data[:-8]
        count_id = data[-8:-4]
        mic = data[-4:]

        # nonce: mac [6], uuid16 [2], count_id [4] (6+2+4 = 12 bytes)
        nonce = b"".join([bthome_mac, uuid, count_id])
        cipher = AES.new(self.bindkey, AES.MODE_CCM, nonce=nonce, mac_len=4)
        cipher.update(b"\x11")

        # decrypt the data
        try:
            decrypted_payload = cipher.decrypt_and_verify(encrypted_payload, mic)
        except ValueError as error:
            self.bindkey_verified = False
            _LOGGER.warning("Decryption failed: %s", error)
            _LOGGER.debug("mic: %s", mic.hex())
            _LOGGER.debug("nonce: %s", nonce.hex())
            _LOGGER.debug("encrypted_payload: %s", encrypted_payload.hex())
            raise ValueError
        if decrypted_payload is None:
            self.bindkey_verified = False
            _LOGGER.error(
                "Decryption failed for %s, decrypted payload is None",
                to_mac(bthome_mac),
            )
            raise ValueError
        self.bindkey_verified = True

        return decrypted_payload
