"""Canonical Omron BLE device profile catalog."""
from __future__ import annotations

from .const import MODERN_STACK_PARENT_SERVICE_UUID
from .devices import (
    BondPolicy,
    ConnectType,
    DeviceConfig,
    Endianness,
    HostPairingMode,
    RecordParser,
    TimeSyncLayout,
    UnlockMode,
)

# Bond strategy for the whole WLD3.0 family (HEM-7380T1 / 7382T1 / 7386T1 /
# 7188T1 / 7155T-MW3). These cuffs serve data during the pairing session and
# then reject later connections, which fits a device that does not keep its
# side of the bond; PER_SESSION drops ours to match, so every connection
# bonds from scratch instead of offering a key the device has forgotten.
#
# Set this to BondPolicy.REUSE to put the family back on a kept bond — that
# is the only change needed, every dependent behaviour is derived from it.
_WLD3_BOND_POLICY = BondPolicy.PER_SESSION

# Whether WLD3.0/WLD4.0 profiles leave their notify CCCDs enabled at session
# close instead of writing 0x0000 to them.
#
# Counted across every ATT write in both phone captures we have -- issue #91
# (BP5465) and issue #67 (HEM-7155T), four sessions, pairing and retained-bond
# alike -- the official app writes 0x0100 to the vendor CCCDs and 0x0002 to
# Service Changed, and never writes 0x0000 to any CCCD. It just disconnects.
# Disables: zero. This integration writes six CCCD values per session, the last
# of them after the session-close command, as the final GATT operation before
# the link drops.
#
# The spec has a peripheral keep CCCD configuration per bonded client (Vol 3
# Part G, 3.3.3.3) and small stacks commonly store it inside the bond record,
# which would make that last write the one thing we do to persistent per-bond
# state that the app never does.
#
# Family-wide rather than per profile because it only ever removes a write, and
# because the evidence spans two device families rather than one. It is not on
# its own a fix for a profile still on PER_SESSION -- that path deletes the bond
# deliberately -- but it stops the divergence either way.
_WLD_KEEP_NOTIFY_SUBSCRIPTIONS = True

# Bond strategy for HEM-7386T1 alone, split out of _WLD3_BOND_POLICY to test
# one device without moving the rest of the WLD3.0 family.
#
# A phone HCI capture of BP5465 (HEM-7382T1-AZAZ, under this profile) reported
# in issue #91 shows the official app doing no pairing at all on a normal sync:
# the cuff raises an SMP Security Request ~53 ms after connect, the phone
# answers with LE Start Encryption from the bond it already holds, and the data
# then flows over the same vendor path this integration uses — writes on GATT
# handle 0x001E (db5b55e0) with notifications on 0x0020 (49123040). No pairing
# request, no key distribution.
#
# PER_SESSION removes exactly that credential after every session, which would
# leave the next normal-mode connection with nothing to resume encryption from.
#
# Not a re-run of the WLD4.0 experiment: that one moved HEM-7188T1 and came
# back negative, but 7188T1 speaks the application-layer secure session and
# this profile does not. And this profile has never been tried on REUSE *with*
# pair_on_connect — it kept its bond via os_bond_once until 2.6.0, in builds
# where pair_on_connect did not exist yet.
#
# On a proxy, pair_on_connect stays true and is what should resume encryption:
# ESPHome's pair request on an already-bonded peer starts encryption from the
# stored key rather than re-pairing, which is the phone's behaviour above. The
# open risk is multi-proxy setups, where the reconnect has to reach the proxy
# that owns the bond — hence the connect-path logging that ships with this.
_WLD3_EXPERIMENT_BOND_POLICY = BondPolicy.REUSE

# Send the connect-time pair request only when a bond has to be created.
#
# On an ESP32 proxy a pair request that does not complete is not free: ESP-IDF
# routes SMP_CONN_TOUT (102) through the default branch of
# ``btc_dm_ble_auth_cmpl_evt`` and deletes the stored bond from flash. Proxy
# logs from issue #91 show a bond present right after pairing
# ("bonded=YES (1 bond(s) stored)") and gone minutes later, after which every
# connection re-pairs from scratch and the cuff — no longer in pairing mode —
# drops the link.
#
# So one failed request costs the credential permanently. Polls now connect
# plain and let the cuff raise its own Security Request, which is what the
# phone capture shows the official app doing: Security Request 16-36 ms after
# connect, then encryption resumed from the retained bond, no pairing.
#
# The attempt count comes down with it: with the bond at stake, one poll
# should be one observation rather than three chances to lose it.
_WLD3_EXPERIMENT_PAIR_ONLY_WHEN_PAIRING = True

# Let the cuff end its own session instead of hanging up on it.
#
# A phone HCI capture of BP5465 in issue #91 shows the official app going
# quiet after its last read and the cuff dropping the link about three
# seconds later (HCI 0x13, remote user terminated). This integration
# disconnected in the same millisecond as the final notification, so every
# session ended 0x16 - closed by us.
#
# Two symptoms fit a cuff that finalises a transfer at its own session end:
# the unread-records counter that only grows across sessions whose data was
# read fine (1 -> 4 over the 24 August captures), and the bond it does not
# honour on the next connection even though both sides now complete key
# distribution. Five seconds covers the phone's three with margin.
_WLD3_EXPERIMENT_PEER_CLOSES_SESSION_SEC = 5.0

# Subscribe to Service Changed while pairing, the way the official app does.
#
# The phone capture in issue #67 (same WLD3.0 profile family) writes 0x0002 to
# handle 0x000B in both of its pairing sessions and in neither of its
# reconnects. The capture's own service discovery places that handle in the
# Generic Attribute service (0x0008-0x000B, uuid 0x1801), whose only
# characteristic is Service Changed - so the write is that characteristic's
# client configuration, enabling indications.
#
# The spec has a peripheral keep that configuration per bonded client. A
# client that writes none may leave it with nothing worth committing, which
# is what a cuff refusing a resumed connection ("PIN or Key Missing") after a
# complete key distribution looks like.
_WLD3_EXPERIMENT_SUBSCRIBE_SERVICE_CHANGED = True

# The settings above as one set, so the profiles under test cannot drift apart.
# Spread into a profile with ``**_WLD3_BOND_EXPERIMENT``; every other profile
# keeps _WLD3_BOND_POLICY and the dataclass defaults.
_WLD3_BOND_EXPERIMENT = {
    "bond_policy": _WLD3_EXPERIMENT_BOND_POLICY,
    "pair_only_when_pairing": _WLD3_EXPERIMENT_PAIR_ONLY_WHEN_PAIRING,
    "connect_settle_attempts": 1,
    "peer_closes_session_sec": _WLD3_EXPERIMENT_PEER_CLOSES_SESSION_SEC,
    "subscribe_service_changed": _WLD3_EXPERIMENT_SUBSCRIBE_SERVICE_CHANGED,
}

_MODERN_OS_BONDING_BASE = {
    "parent_service_uuid": MODERN_STACK_PARENT_SERVICE_UUID,
    "rx_channel_uuids": ["49123040-aee8-11e1-a74d-0002a5d5c51b"],
    "tx_channel_uuids": ["db5b55e0-aee7-11e1-965e-0002a5d5c51b"],
    "host_pairing_mode": HostPairingMode.OS_BONDING,
}

CANONICAL_DEVICE_PROFILES: dict[str, DeviceConfig] = {
    "HEM-6320T": DeviceConfig(
        model="HEM-6320T",
        endianness=Endianness.BIG,
        user_start_addresses=[0x0370],
        per_user_records_count=[100],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0F74,
        settings_write_address=0x0F9A,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.LINEAR_10,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-6320T-SH",
            "HEM-6320T-Z",
            "HEM-6322T-SH",
            "HEM-6323T",
            "HEM-6325T",
        ),
    ),
    "HEM-6321T": DeviceConfig(
        model="HEM-6321T",
        endianness=Endianness.BIG,
        user_start_addresses=[0x0370, 0x08E8],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0F74,
        settings_write_address=0x0F9A,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.LINEAR_10,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-6321T-Z",
            "HEM-6324T",
        ),
    ),
    "HEM-6401T": DeviceConfig(
        model="HEM-6401T",
        endianness=Endianness.LITTLE,
        # HEM-6401T exposes multiple data types; only the BP data_5 area is mapped here.
        user_start_addresses=[0x1350],
        per_user_records_count=[100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0100,
        settings_write_address=0x0160,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x10, 0x20],
        time_sync_layout=TimeSyncLayout.HEM6401_PREFIX,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x06, "unread_counter_offset": 0x0E, "write_cursor_mask": 0x3FFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": 0},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_16_6401_FAMILY,
        equivalent_model_ids=(
            "HEM-6401T-Z",
            "HEM-6402T-Z",
        ),
    ),
    "HEM-6410T": DeviceConfig(
        model="HEM-6410T",
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x5590],
        per_user_records_count=[100],
        record_byte_size=0x20,
        transmission_block_size=0x10,
        settings_read_address=0x0100,
        settings_write_address=0x0170,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x10, 0x20],
        time_sync_layout=TimeSyncLayout.HEM6401_PREFIX,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x06, "unread_counter_offset": 0x0E, "write_cursor_mask": 0x3FFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": 0},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_16_6401_FAMILY,
        equivalent_model_ids=(
            "HEM-6410T-Z",
            "HEM-6410T-Z_BP",
            "HEM-6410T-Z_BP+EV",
            "HEM-6411T-MAE",
        ),
    ),
    "HEM-7320T": DeviceConfig(
        model="HEM-7320T",
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC, 0x05F4],
        per_user_records_count=[60, 60],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.LINEAR_10,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7320T-CA",
            "HEM-7320T-CACS",
            "HEM-7320T-ZV",
            "HEM-7320T_TI-CA",
            "HEM-7320T_TI-Z",
            "HEM-8725T-WM",
        ),
    ),
    "HEM-7322T": DeviceConfig(
        model="HEM-7322T",
        connect_type=ConnectType.WLB1_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC, 0x0824],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.CLASSIC_MIXED,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-7321T-CA",
            "HEM-7321T_TI-CA",
            "HEM-7321T_TI-Z",
            "HEM-7280T-AP",
            "HEM-7280T-D",
            "HEM-7280T-E",
            "HEM-7280T_TI-D",
            "HEM-7280T_TI-E",
            "HEM-7281T",
            "HEM-7282T",
            "HEM-7321T-ZV",
            "HEM-7322T-D",
            "HEM-7322T-E",
        ),
    ),
    "HEM-7511T": DeviceConfig(
        model="HEM-7511T",
        connect_type=ConnectType.WLB1_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC, 0x0798],
        per_user_records_count=[90, 90],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.CLASSIC_MIXED,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 89, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 89, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
    ),
    "HEM-8732T": DeviceConfig(
        model="HEM-8732T",
        connect_type=ConnectType.WLB1_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC, 0x05F4],
        per_user_records_count=[60, 60],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.CLASSIC_MIXED,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-8732T-SH",
        ),
    ),
    "HEM-8732K": DeviceConfig(
        model="HEM-8732K",
        connect_type=ConnectType.WLB1_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC, 0x0522],
        per_user_records_count=[45, 45],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.CLASSIC_MIXED,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 44, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 44, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-8732K-SH",
        ),
    ),
    "HEM-7600T": DeviceConfig(
        model="HEM-7600T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC],
        per_user_records_count=[100],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        # classic-mixed field order (confirmed from HEM-7600T-E EEPROM dumps)
        time_sync_layout=TimeSyncLayout.CLASSIC_MIXED,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        # bit-packed big-endian record layout (confirmed from HEM-7600T-E:
        # byte-aligned classic_vital_14 yielded dia>sys / bpm=26 / no date)
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-7270C",
            "HEM-7271T",
            "HEM-7600T",
            "HEM-7600T-E",
            "HEM-7600T-Z",
            "HEM-7600T-ZCD6BK",
            "HEM-7600T-SH3BK",
            "HEM-7600T2-JF",
            "HEM-7600T_W",
            "HEM-7600T_W-SH3W",
            "HEM-7600T_W-Z",
        ),
    ),
    # HEM-9601T ("HeartGuide" watch-style blood pressure monitor) — 24-byte records
    # at 0x041A (350 slots), single-user, index size 20 at 0x0356, WLS3.0.
    # Note: Profile is derived from vendor specs (reverseSendData=1, word-swap group)
    # and marked unverified until validated with real-device EEPROM dump / logs.
    "HEM-9601T": DeviceConfig(
        model="HEM-9601T",
        connect_type=ConnectType.WLS3_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x041A],
        per_user_records_count=[350],
        record_byte_size=0x18,
        transmission_block_size=0x2C,
        settings_read_address=0x0356,
        settings_write_address=0x03B8,
        settings_unread_records_bytes=[0x00, 0x14],
        settings_time_sync_bytes=[0x50, 0x5A],  # Window 80..90 in Block 3 (72..90); clock at 82..87 (cached[2:8])
        time_sync_layout=TimeSyncLayout.LINEAR_10,
        index_pointer_layout={
            "index_region_byte_size": 0x14,
            "endianness": "big",
            "backtrack_slots": 5,
            "users": [
                {
                    "write_cursor_offset": 0x00,
                    "unread_counter_offset": 0x04,
                    "write_cursor_mask": 0x3FFF,
                    "slot_index_min": 0,
                    "slot_index_max": 349,
                    "slot_index_bias": -1,
                    "clear_value": 0x8000,
                },
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_24_HEARTGUIDE,
        equivalent_model_ids=(
            "HEM-9601T-J3",
            "HEM-9601T2-BR3",
            "HEM-9601T_E3",
        ),
    ),
    # HEM-9700T ("HeartGuide" 1000-slot variant)
    # Note: Profile is derived from vendor specs and marked unverified until validated.
    "HEM-9700T": DeviceConfig(
        model="HEM-9700T",
        connect_type=ConnectType.WLS3_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x041A],
        per_user_records_count=[1000],
        record_byte_size=0x18,
        transmission_block_size=0x2C,
        settings_read_address=0x0356,
        settings_write_address=0x03B8,
        settings_unread_records_bytes=[0x00, 0x14],
        settings_time_sync_bytes=[0x50, 0x5A],  # Window 80..90 in Block 3 (72..90); clock at 82..87 (cached[2:8])
        time_sync_layout=TimeSyncLayout.LINEAR_10,
        index_pointer_layout={
            "index_region_byte_size": 0x14,
            "endianness": "big",
            "backtrack_slots": 5,
            "users": [
                {
                    "write_cursor_offset": 0x00,
                    "unread_counter_offset": 0x04,
                    "write_cursor_mask": 0x3FFF,
                    "slot_index_min": 0,
                    "slot_index_max": 999,
                    "slot_index_bias": -1,
                    "clear_value": 0x8000,
                },
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_24_HEARTGUIDE,
    ),
    "HEM-7325T": DeviceConfig(
        model="HEM-7325T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02AC],
        per_user_records_count=[90],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x0286,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x14, 0x1E],
        time_sync_layout=TimeSyncLayout.CLASSIC_MIXED,
        index_pointer_layout={
            "index_region_byte_size": 0x08,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 89, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
    ),
    "HEM-6232T": DeviceConfig(
        model="HEM-6232T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02E8, 0x0860],
        per_user_records_count=[100, 100],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=[0x00, 0x08],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.CLASSIC_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_6232_FAMILY,
        equivalent_model_ids=(
            "HEM-6232T-AP",
            "HEM-6232T-D",
            "HEM-6232T-E",
            "HEM-6232T-Z",
            "HEM-6233T",
        ),
    ),
    "HEM-1026T2": DeviceConfig(
        model="HEM-1026T2",
        connect_type=ConnectType.WLD2_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x02E8, 0x0928],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-1026T2-AJC",
            "HEM-1026T2-AJE",
            "HEM-1026T2-AKA",
        ),
    ),
    "HEM-7530T": DeviceConfig(
        model="HEM-7530T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02E8],
        per_user_records_count=[90],
        record_byte_size=0x0E,
        transmission_block_size=0x10,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        # Unread counter unsupported; time sync at [0x2C, 0x3C] uses classic offset8
        # field order (same 16-byte block as HEM-6232T, not modern linear order).
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.CLASSIC_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 89, "slot_index_bias": -1},
            ],
        },
        # bit-packed record layout (confirmed from HEM-6161T-RU; byte-aligned
        # classic_vital_14 yields bpm=26 and no valid datetime on this family).
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-6231T2-JC",
            "HEM-6231T2-JE",
            "HEM-6231T2-JT3",
            "HEM-7271P-SH3",
            "HEM-7271T_SH3",
            "HEM-7530T-Z",
            "HEM-7530T1-BR3",
            "HEM-7530T_AP3",
            "HEM-7530T_E3",
            "HEM-7530T_J3",
            "HEM-7530T_JT3",
            "HEM-8630T-SH",
        ),
    ),
    # Single-user 30-slot variant of the 7530T EEPROM family (0x260/0x2E8).
    "HEM-6161T": DeviceConfig(
        model="HEM-6161T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02E8],
        per_user_records_count=[30],
        record_byte_size=0x0E,
        transmission_block_size=0x10,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.CLASSIC_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 29, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-6161T-D",
            "HEM-6161T-E",
            "HEM-6161T-RU",
            "HEM-6161T2-BR",
            "HEM-7271L-SH3",
        ),
    ),
    # Single-user 60-slot variant of the 7530T EEPROM family (0x260/0x2E8).
    "HEM-7136T": DeviceConfig(
        model="HEM-7136T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02E8],
        per_user_records_count=[60],
        record_byte_size=0x0E,
        transmission_block_size=0x10,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.CLASSIC_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-7136T-SH3",
            "HEM-7138JT-SH",
            "HEM-7138T-SH",
            "HEM-7139T-SH3",
        ),
    ),
    # Single-user 100-slot variant of the 7530T EEPROM family (0x260/0x2E8).
    "HEM-6231T": DeviceConfig(
        model="HEM-6231T",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02E8],
        per_user_records_count=[100],
        record_byte_size=0x0E,
        transmission_block_size=0x10,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.CLASSIC_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
        equivalent_model_ids=(
            "HEM-6231T-SH",
        ),
    ),
    # Single-user 90-slot variant of the 7530T EEPROM family (0x260/0x2E8).
    "HEM-6231T_Z": DeviceConfig(
        model="HEM-6231T_Z",
        aggressive_gatt_timing=True,
        endianness=Endianness.BIG,
        user_start_addresses=[0x02E8],
        per_user_records_count=[90],
        record_byte_size=0x0E,
        transmission_block_size=0x10,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.CLASSIC_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "big",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 89, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14_BITPACKED,
    ),
    # HEM-7153JT: same layout as HEM-7150T but has 30 record slots
    "HEM-7153JT": DeviceConfig(
        model="HEM-7153JT",
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098],
        per_user_records_count=[30],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 29, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7153JT_ASH",
        ),
    ),
    "HEM-7150T": DeviceConfig(
        model="HEM-7150T",
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098],
        per_user_records_count=[60],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7150T-CA",
            "HEM-7150T-Z",
            "HEM-7153T_ASH",
            "HEM-7156T-BR",
            "HEM-7156T-LA",
            "HEM-7156T_AAP",
            "HEM-7156T_AP",
        ),
    ),
    # HEM-7157T / HEM-7158T: same layout as HEM-7150T but has 100 record slots
    "HEM-7157T": DeviceConfig(
        model="HEM-7157T",
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098],
        per_user_records_count=[100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7157T-AP",
            "HEM-7158T-JC",
            "HEM-7158T_AP3",
        ),
    ),
    # HEM-7151T: same layout as HEM-7150T but has 80 record slots
    "HEM-7151T": DeviceConfig(
        model="HEM-7151T",
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098],
        per_user_records_count=[80],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 79, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7151T-Z",
        ),
    ),
    "HEM-7155T": DeviceConfig(
        model="HEM-7155T",
        connect_type=ConnectType.WLS3_0,
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098, 0x0458],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7155T-ALRU",
            "HEM-7155T-D",
            "HEM-7155T-EBK",
            "HEM-7155T-EBL",
            "HEM-7155T_AP",
            "HEM-7155T_ASH3BK",
            "HEM-7155T_ASH3SL",
            # Classic-stack X4 Smart. The modern-firmware variant is
            # "HEM-7155T_ESL1" under the HEM-7155T-MW3 profile.
            "HEM-7155T_ESL",
            "HEM-7340T-CA",
            "HEM-7340T-Z",
            "HEM-7341T-Z",
        ),
    ),
    # HEM-7155T modern stack V2 — OS bonding only, same EEPROM addresses as V1
    "HEM-7155T-MW": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7155T-MW",
        connect_type=ConnectType.WLS3_0,
        unlock_mode=UnlockMode.NONE,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098, 0x0458],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x2C,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        # EEPROM time sync confirmed via omblepy hem-7155t.py (settings block at
        # 0x0010, time bytes [0x2C, 0x3C], modern offset8 layout). Addresses
        # match the classic HEM-7155T V1 profile exactly.
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
    ),
    # HEM-7155T K4 — modern stack, MW3 EEPROM layout, no secure session.
    "HEM-7155T-K4": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7155T-K4",
        connect_type=ConnectType.WLD2_0,
        unlock_mode=UnlockMode.NONE,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x02E8, 0x06A8],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7155T_K4-D",
            "HEM-7155T_K4-EBK",
            "HEM-7155T_K4-ESL",
            "HEM-7340T_K4-CA",
            "HEM-7340T_K4-Z",
            "HEM-7341T_K4-Z",
        ),
    ),
    # HEM-7155T modern stack V3 — OS bonding + stateless token handshake.
    # Confirmed via HCI btsnoop of HEM-7155T-ESL ("X4 Smart"): a Just Works bond
    # plus a 0x11/0x91 token handshake (host sends 0x11 + 4 nonce bytes, device
    # echoes them in a 0x91 0x00 ack), then plaintext 08-frame memory protocol
    # with XOR CRC — NOT the app-layer secure session (no ECDH/AES-CCM).
    # The token is required for memory access outside the device's -P- pairing
    # grace window: a normal-mode poll without it returns no data.
    "HEM-7155T-MW3": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7155T-MW3",
        # Modern-fe4a-firmware HEM-7155T_ESL ("X4 Smart"); WLD3.0 like 7380T1.
        connect_type=ConnectType.WLD3_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        bond_policy=_WLD3_BOND_POLICY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x02E8, 0x06A8],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        # EEPROM time sync: the V3 settings block moved to 0x0260, but the
        # time-bytes slice [0x2C, 0x3C] is a block-relative offset (same as the
        # 7155T family) so it applies unchanged. EEPROM sync runs before CTS and
        # falls back to it, so CTS still works if these offsets are wrong.
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            # Index entries are 8 bytes per user (uint32 write cursor + uint32
            # unread counter), confirmed via HCI btsnoop of HEM-7155T-ESL:
            # user1 cursor at 0x00, user2 cursor at 0x08 (NOT 0x02 — that offset
            # lands in the high half of user1's word and reads the 0x80 flag).
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x08, "unread_counter_offset": 0x0C, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            # HEM-7155T_ESL1 ("X4 Smart", modern firmware): modern FE4A stack
            # with V3 EEPROM at 0x0260, classic plaintext transport over a Just
            # Works bond (NOT secure session) plus a 0x11/0x91 token unlock —
            # confirmed via HCI btsnoop. The classic-stack HEM-7155T_ESL maps to
            # the "HEM-7155T" profile instead.
            "HEM-7155T_ESL1",
        ),
    ),
    # HEM-7146T modern stack — OS bonding only, 1 user, 30 records
    "HEM-7146T": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7146T",
        connect_type=ConnectType.WLD1_0,
        unlock_mode=UnlockMode.NONE,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x02E8],
        per_user_records_count=[30],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 29, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7143T1-AIN",
            "HEM-7143T1-AP",
            "HEM-7143T1-D",
            "HEM-7143T1-E",
            "HEM-7143T1_D",
            "HEM-7143T1_EBK",
            "HEM-7143T2-E",
            "HEM-7143T2_ESL",
            "HEM-7144T1-AU",
            "HEM-7144T2-BR",
            "HEM-7144T2-LA",
            "HEM-7146T2",
            "HEM-7146T2-EBK",
            "HEM-7146T2-ESL",
            "HEM-7146T2-JD",
            "HEM-7146T2-JF",
            "HEM-716DT2-LA",
        ),
    ),
    "HEM-7342T": DeviceConfig(
        model="HEM-7342T",
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098, 0x06D8],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7159T_AP3",
            "HEM-7342T-CA",
            "HEM-7342T-Z",
            "HEM-7342T1-ACACD6",
            "HEM-7343T",
            "HEM-7343T-Z",
            "HEM-7344JT_ASH3",
            "HEM-7344T_ASH3BK",
            "HEM-7344T_ASH3SL",
            "HEM-7346T-AJC3",
            "HEM-7346T-AJE3",
            "HEM-7346T2-AJC32",
            "HEM-7346T2-AJE32",
            "HEM-7346T_ABR3",
            "HEM-7346T_AP3",
            "HEM-7347T-AJC3",
            "HEM-7347T-AJE3",
            "HEM-7347T2-AJC32",
            "HEM-7347T2-AJE32",
            "HEM-7349T_ABR",
            "HEM-7361T-ALRU",
            "HEM-7361T-AP",
            "HEM-7361T-D",
            "HEM-7361T-EBK",
            "HEM-7361T1-BS",
            "HEM-7361T_ESL",
        ),
    ),
    "HEM-7361T": DeviceConfig(
        model="HEM-7361T",
        aggressive_gatt_timing=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x0098, 0x06D8],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x10,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=[0x00, 0x10],
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
    ),
    # HEM-7191T1 family ("M3 Comfort AFib" / "X3 Comfort AFib") —
    # 1-user, 60 records (data_1=0x01C4) on WLD4.0 transport with token-key unlock.
    "HEM-7191T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7191T1",
        connect_type=ConnectType.WLD4_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        bond_policy=_WLD3_BOND_POLICY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x01C4],
        per_user_records_count=[60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7191T1-LZ",
        ),
    ),
    # HEM-7196T1 / HEM-7194T1 family ("M4 Connect AFib" / "X4 Connect AFib" etc.) —
    # 2-user, 60 records per user (data_1=0x01C4, data_2=0x0584) on WLD4.0
    # transport with token-key unlock.
    "HEM-7196T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7196T1",
        connect_type=ConnectType.WLD4_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        bond_policy=_WLD3_BOND_POLICY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x01C4, 0x0584],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7194T1-FLAP",
            "HEM-7194T1-FLCAP",
            "HEM-7194T1_FLBIN",
            "HEM-7194T1_FLIN",
            "HEM-7196T1-FLE",
            "HEM-7196T1-FLEO",
        ),
    ),
    "HEM-7380T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7380T1",
        connect_type=ConnectType.WLD3_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        # Same protocol family as HEM-7386T1 and the same reported symptom
        # (issue #20), so it runs the same experiment rather than a variant.
        **_WLD3_BOND_EXPERIMENT,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x01C4, 0x0804],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7380T",
            "HEM-7380T1-EBK",
            "HEM-7380T1-EOSL",
            "HEM-7383T1-AP",
            "HEM-7384T1-NBBR",
        ),
    ),
    # HEM-7376T1 / -AJAZ3/-JM3 siblings: 2-user, 60 records per user
    # (data_1=0x080C, data_2=0x0BCC), write address 0x0058, +4 time offset ([0x30, 0x40]).
    "HEM-7376T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7376T1",
        connect_type=ConnectType.WLD3_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        bond_policy=_WLD3_BOND_POLICY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x080C, 0x0BCC],
        per_user_records_count=[60, 60],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0058,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x30, 0x40],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x1C,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 59, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7376T1-ACACD6",
            "HEM-7376T1-Z",
            "HEM-7385T1-AJAZ3",
            "HEM-7387T1-AJAZ3",
            "HEM-7389T1-JM3",
        ),
    ),
    # HEM-7377T1 family (7 Series Upper Arm / BP5360) — 2-user, 80 records per user (data_1=0x080C,
    # data_2=0x0D0C), write address 0x0058, +4 time offset ([0x30, 0x40]).
    "HEM-7377T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7377T1",
        connect_type=ConnectType.WLD3_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        bond_policy=_WLD3_BOND_POLICY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x080C, 0x0D0C],
        per_user_records_count=[80, 80],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0058,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x30, 0x40],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x1C,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 79, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 79, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7377T1-ZAZ",
        ),
    ),
    # HEM-7386T1 family (HEM-7386T1, HEM-7388T1, HEM-7381T1-AZ, HEM-7382T1-AZAZ):
    # 2-user, 100 records per user (data_1=0x080C, data_2=0x0E4C), write address 0x0058,
    # +4 time offset ([0x30, 0x40]). HEM-7382T1-AZAZ maps to 100 records per user (see hass-omron#91).
    "HEM-7386T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7386T1",
        connect_type=ConnectType.WLD3_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        **_WLD3_BOND_EXPERIMENT,
        # This profile alone: BP5465 is the only device under it with a phone
        # capture that shows the two end-of-session mirror writes and the exact
        # addresses they land on (issue #91). HEM-7376T1/HEM-7377T1 share the
        # 0x0058 mirror base and HEM-7380T1 shares the family, but their offsets
        # are inferred, so they stay off until someone captures one.
        session_ack_mirror_writes=True,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x080C, 0x0E4C],
        per_user_records_count=[100, 100],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0058,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x30, 0x40],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x1C,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
                {"write_cursor_offset": 0x02, "unread_counter_offset": 0x06, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 99, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7381T1-AZ",
            "HEM-7382T1",
            "HEM-7382T1-AZAZ",
            "HEM-7386T1-AJF3",
            "HEM-7388T1-AJF3",
        ),
    ),
    # HEM-7188T1 / HEM-7183T1 family ("X2+ Connect" / "M2+" etc.) — dedicated single-user profile
    # with WLD4.0 transport (ConnectType.WLD4_0) and 30-slot 16-byte record
    # layout at 0x01C4 with index pointer layout at 0x0010.
    # Operationally uses token-key transport (UnlockMode.TOKEN_KEY): an
    # application-layer ECDH secure session (0x70 0x01) was tried, but real
    # devices reject the pairing request with error frame 0xff26, while the
    # plaintext token-key path reads real measurement values. Revisit
    # unlock_mode=SECURE_SESSION once the ECDH rejection is understood.
    "HEM-7188T1": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7188T1",
        connect_type=ConnectType.WLD4_0,
        keep_notify_subscriptions=_WLD_KEEP_NOTIFY_SUBSCRIPTIONS,
        unlock_mode=UnlockMode.TOKEN_KEY,
        bond_policy=_WLD3_BOND_POLICY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x01C4],
        per_user_records_count=[30],
        record_byte_size=0x10,
        transmission_block_size=0x38,
        settings_read_address=0x0010,
        settings_write_address=0x0054,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x18,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 29, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7183T1-AP",
            "HEM-7183T1-CAP",
            "HEM-7183T1_FLBIN",
            "HEM-7183T1_FLIN",
            "HEM-7183T1_LAP",
            "HEM-7188T1-LE",
            "HEM-7188T1-LEO",
        ),
    ),
    # HEM-716BT2 family (30-slot variant of HEM-7142T2 / WLD1.0 stack)
    "HEM-716BT2": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-716BT2",
        connect_type=ConnectType.WLD1_0,
        unlock_mode=UnlockMode.TOKEN_KEY,
        endianness=Endianness.LITTLE,
        user_start_addresses=[0x02E8],
        per_user_records_count=[30],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 29, "slot_index_bias": -1},
            ],
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7149T2-E",
            "HEM-716BT2-ZAZ",
            "HEM-716CT2-Z",
        ),
    ),
    # HEM-7142T2 — modern stack, MW3-style EEPROM, stateless 0x11/0x91 token
    # handshake (same pattern as HEM-7155T-MW3).
    "HEM-7142T2": DeviceConfig(
        **_MODERN_OS_BONDING_BASE,
        model="HEM-7142T2",
        connect_type=ConnectType.WLD1_0,
        unlock_mode=UnlockMode.TOKEN_KEY,
        endianness=Endianness.LITTLE,
        # Single on-device measurement buffer region for this profile.
        user_start_addresses=[0x02E8],
        per_user_records_count=[14],
        record_byte_size=0x0E,
        transmission_block_size=0x2C,
        settings_read_address=0x0260,
        settings_write_address=0x02A4,
        settings_unread_records_bytes=None,
        settings_time_sync_bytes=[0x2C, 0x3C],
        time_sync_layout=TimeSyncLayout.MODERN_OFFSET8,
        index_pointer_layout={
            "index_region_byte_size": 0x10,
            "endianness": "little",
            "backtrack_slots": 0,
            "users": [
                {"write_cursor_offset": 0x00, "unread_counter_offset": 0x04, "write_cursor_mask": 0xFF, "slot_index_min": 0, "slot_index_max": 13, "slot_index_bias": -1},
            ],
            "record_addresses": [0x02E8],
            "record_byte_size": 0x0E,
            "record_step": 0x0E,
        },
        record_parser=RecordParser.CLASSIC_VITAL_14,
        equivalent_model_ids=(
            "HEM-7138K-SH",
            "HEM-7140T1-AP",
            "HEM-7141T1-AP",
            "HEM-7142T1-AP",
            "HEM-7142T2-AP",
            "HEM-7142T2-Z",
            "HEM-7142T2-ZAZ",
            "HEM-7142T2_JAZ",
        ),
    ),
}
