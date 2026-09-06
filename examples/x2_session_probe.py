"""Explicit X2+ candidate test, private device-bound key, metadata-only read."""
import argparse
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import types

parser = argparse.ArgumentParser()
parser.add_argument("mode", choices=["pair", "reconnect"])
parser.add_argument("--checkout", type=Path, required=True)
parser.add_argument("--credentials", type=Path, required=True)
parser.add_argument("--address", required=True)
parser.add_argument("--no-connect-pair", action="store_true",
                    help="Diagnostic: use existing OS bond, skip connect-time Pair")
parser.add_argument("--passive-bonding-agent", action="store_true",
                    help="Linux diagnostic: retain BlueZ agent without calling Pair")
args = parser.parse_args()
if args.passive_bonding_agent and (sys.platform != "linux" or not args.no_connect_pair):
    parser.error("--passive-bonding-agent requires Linux and --no-connect-pair")
package = types.ModuleType("omron_ble")
package.__path__ = [str(args.checkout / "custom_components/omron/omron_ble")]
sys.modules["omron_ble"] = package

from bleak import BleakScanner  # noqa: E402
from omron_ble.devices import UnlockMode, get_device_config  # noqa: E402
from omron_ble.omron_driver import OmronDeviceSession, _bluez_pairing_agent  # noqa: E402
from omron_ble.secure_flow import establish_secure_session  # noqa: E402


async def main():
    binding = {"address": args.address.upper(), "host": socket.gethostname()}
    key = None
    if args.mode == "reconnect":
        saved = json.loads(args.credentials.read_text())
        if any(saved.get(k) != v for k, v in binding.items()):
            raise ValueError("Credential device/host mismatch")
        if args.credentials.stat().st_mode & 0o077:
            raise ValueError("Credential permissions must be private")
        key = bytes.fromhex(saved["ltk"])
    elif args.credentials.exists():
        raise ValueError("Pair test will not overwrite an existing credential file")
    config = replace(get_device_config("HEM-7188T1-LEO"),
                     model="HEM-7188T1-LEO", unlock_mode=UnlockMode.SECURE_SESSION)
    if args.no_connect_pair:
        print("DIAGNOSTIC_SKIP_CONNECT_PAIR", flush=True)
    wanted_service = "0000fcb4-0000-1000-8000-00805f9b34fb" if key is None else "0000fe4a-0000-1000-8000-00805f9b34fb"
    async with AsyncExitStack() as resources:
        if args.passive_bonding_agent:
            agent = await resources.enter_async_context(_bluez_pairing_agent())
            if agent is None:
                raise RuntimeError("BlueZ agent unavailable; diagnostic not started")
            print("PASSIVE_BONDING_AGENT_READY; NO_EXPLICIT_PAIR_CALL", flush=True)
        scanner = await resources.enter_async_context(BleakScanner())
        print("SCAN_READY", args.mode, flush=True)
        async with asyncio.timeout(60):
            async for device, advertisement in scanner.advertisement_data():
                if device.address.upper() == binding["address"] and wanted_service in advertisement.service_uuids:
                    break
        print("FOUND_TARGET", flush=True)
        print("ADVERTISEMENT_MODE", "pairing" if
              "0000fcb4-0000-1000-8000-00805f9b34fb" in advertisement.service_uuids
              else "normal", flush=True)
        session = OmronDeviceSession(device, config,
                                     pairing_session=key is None and not args.no_connect_pair)
        try:
            await asyncio.wait_for(session.connect(), timeout=25)
            # Diagnostic changes only connect-time bonding, not the explicit
            # application-level initialization mode selected by the user.
            session._pairing_session = key is None
            print("CONNECTED", flush=True)
            authenticated_key = await establish_secure_session(
                session, stored_ltk=key, now=datetime.now()
            )
            print("X2_AUTH_AND_INITIALIZATION_OK" if key is None else "X2_RESUME_AUTH_OK", flush=True)
            if key is None:
                args.credentials.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temporary = tempfile.mkstemp(dir=args.credentials.parent, prefix=".x2-key-")
                try:
                    with os.fdopen(fd, "w") as out:
                        json.dump({**binding, "ltk": authenticated_key.hex()}, out)
                        out.flush()
                        os.fsync(out.fileno())
                    # No-overwrite publication; temporary file is already mode 0600.
                    os.link(temporary, args.credentials)
                finally:
                    os.unlink(temporary)
                print("CREDENTIAL_COMMITTED_AFTER_CLOSE", flush=True)
            else:
                await session.open_memory_session()
                metadata = await session.read_memory_block(0x0010, 24)
                if len(metadata) != 24:
                    raise ValueError("Incomplete metadata response")
                print("METADATA_READ_OK bytes=24; NO_RECORDS_READ", flush=True)
                await session.close_memory_session()
                if session._last_reply_packet_type != b"\x8f\x00" or session._last_reply_payload != b"\x00":
                    raise ConnectionError("Read close not accepted")
                print("READ_CLOSE_OK", flush=True)
            await asyncio.sleep(1.2)
        finally:
            await session.aclose()
            print("CONNECTION_CLOSED", flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("omron_ble.secure_flow").setLevel(logging.INFO)
    asyncio.run(asyncio.wait_for(main(), timeout=150))
