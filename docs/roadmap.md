# Roadmap

Where the code stands after the 2.10.2 cleanup, what was deliberately left
for later, and what the open issues are waiting on. Update this when a listed
item lands or its trigger changes.

## Layout as of 2.10.2

`custom_components/omron/` is the Home Assistant integration;
`custom_components/omron/omron_ble/` is the vendored protocol library. The
library depends on `bleak`, `bleak-retry-connector`, `blesession`,
`sensor-state-data` and (optionally) `dbus_fast` / `cryptography` — never
on `homeassistant`. `blesession.hass` is imported only from the integration
package. The bond-settle loop, the pairing agent and the memory protocol
stay here; the stage trace, the radio facts and the failure report do not.
`tests/test_omron_ble_boundary.py` enforces that.

```
bluez ─┐
       ├─ connection ─┐
const ─┤              ├─ session (+ memory_protocol mixin) ── driver ── time_sync ── pairing
devices┤              │                                                                  │
secure_session ── secure_flow ─┘                                                        │
settings_mirror ──────┘                                                  parser ◄────────┘
```

| Module | Holds |
|---|---|
| `bluez.py` / `bluez_agent.py` | D-Bus helpers (agent, Pair, RemoveDevice, Paired) and the `dbus_fast` agent class the former imports lazily |
| `session_trace.py` | Cuff stage names mapped onto `blesession`'s vocabulary |
| `connection.py` | `establish_connection_with_bond_settle`, bleak cache helpers |
| `session.py` | `OmronDeviceSession`: connection lifecycle, unlock, pairing |
| `memory_protocol.py` | `MemoryProtocolMixin`: RX notify channels, command/reply, memory session, pairing registration |
| `driver.py` | `OmronDeviceDriver`: EEPROM time, index walk, latest-record selection |
| `secure_session.py` / `secure_flow.py` | ECDH + AES-CCM state machine, and the handshake that drives it |
| `settings_mirror.py` | The settings block a session writes back before closing |
| `time_sync.py` | CTS / EEPROM clock writes (setup and every poll) |
| `pairing.py` | Model-number probe and `async_pair_and_sync_device` (config-flow procedures) |
| `parser.py` | `OmronBluetoothDeviceData`: advertisement parse, `async_poll`, sensor publishing — the HA `*-ble` library convention |
| `devices.py` / `device_catalog.py` / `model_aliases.py` | Profile schema, the per-model catalog, name aliases |
| `record_parsers.py` | Pure record decoders |

HA side: `__init__.py` (setup, poll wrapper, advertisement callback),
`config_flow.py`, `session_handoff.py` (parking an open session between the
config flow and the first poll), entity platforms.

## Done in the 2.10.2 cleanup

All five were move-only: every definition's AST was compared against its old
location, and tests that patch or read source were repointed.

| PR | Change |
|---|---|
| #182 | `omron_ble` no longer imports `homeassistant`; time zone injected as a getter; boundary test |
| #183 | `omron_driver.py` (3,300 lines, three layers) → `bluez` / `connection` / `session` / `driver`; `secure_flow` cycle broken |
| #184 | `ble_session` → `session_handoff`, `setup_time_sync` → `time_sync`, `setup` → `pairing` |
| #185 | `settings_mirror` errors name the mirror, not the secure flow |
| #186 | Memory protocol out of `OmronDeviceSession` into `MemoryProtocolMixin` |

## Deferred cleanup — do when the trigger fires, not before

Each of these is a real improvement whose cost is not yet justified. The
trigger is the moment it becomes cheaper to do than not to do.

| Item | Trigger | Notes |
|---|---|---|
| Split unlock out of `session.py`; move `_start_notify_with_recovery` and `_NOTIFY_SUBSCRIBE_SETTLE_SEC` to `connection.py` | The next unlock or pairing fix that touches `session.py` | The notify-recovery helper is shared by memory protocol, `_token_unlock` and `_pair_custom_key`; it is link-layer code that #186 parked in the mixin |
| `MemoryProtocolMixin` → an owned component | Wanting to unit-test the memory protocol without a session | Behavioural change, not move-only: tests and `examples/x2_session_probe.py` read `session._last_reply_*` directly; `open_memory_session` resets `_unlocked` on failure (cross-layer); `_on_notify_channel_data` decrypts via `_secure_session`. A `reset()` on the component must `clear()` the reply Event, never replace it |
| Extract `_decode_omron_msd_fields` → `advertisement.py`, BLS/RACP helpers → `bls.py` | A bug in MSD or BLS decoding that needs a standalone test | Both are pure; `parser.py` keeps its HA-convention name and role |
| Shorten the long functions | Only when a fix already touches one | `driver._get_latest_via_index` 292, `__init__.process_service_info` 288, `__init__.async_setup_entry` 268 (defines `_async_poll_data` and two others as closures — untestable in isolation), `parser._poll_device_readout` 252, `parser.async_poll` 237, `session._pair_custom_key` 178. Function extraction cannot be proven move-only the way module moves were, so it rides along with hardware-verified fixes |

Not planned: renaming `parser.py`. It matches the HA `*-ble` library
convention (`xxx_ble/parser.py` holding the `BluetoothData` subclass) and would
matter if the library is ever published on its own.

## Known, unreported

**Cuff clock vs. record time zone.** Every clock write to the cuff uses the
process's OS zone (`dt.datetime.now().astimezone()` in `time_sync.py`,
`session.py`, `driver.py`), while `parser._ensure_aware_datetime` labels the
naive record timestamps with the HA zone. HA does not set `TZ`, so a container
on UTC with HA configured for a local zone stores UTC wall-clock on the cuff
and then reads it back as local — off by the offset. Not touched in the
cleanup; fix by injecting `now` from the HA layer into the time-sync path (the
same route #182 used for `get_tz`), not by changing the parser. Waiting for a
report before acting.

## Open issues (as of 2.10.2, not yet triaged here)

Grouped by what they probably share; the grouping is a starting point, not a
diagnosis.

| Group | Issues |
|---|---|
| Proxy path | #174 works via ESP32 proxy, fails via Shelly 1 Gen4 — recent and specific; `connection.py` / `bluez.py` is where the proxy-vs-local decisions now live |
| HEM-7380T family | #133 (7380T1-EBK, everything unknown), #20 (7380T1-EOSL), #7 (7380T-EBK pairing failed) — three reports on one family, worth one investigation |
| New model | #132 HEM-7361T — catalog addition once a capture or profile source exists |
| Pairing / refresh on specific models | #67 (7155T-ESLI), #62 (BP7365CAN / 7376T1), #45 (7196T1-FLE refresh), #39 (BP5450 / 7342T no refresh) |
| Stale | #2 "Failed to start?" — needs a reproduction request or closing |

Suggested order: #174, then the 7380T family together.

## How to keep it this way

- `omron_ble` imports nothing from `homeassistant` or from its parent package. The boundary test fails otherwise.
- Structural refactors are move-only, in their own PR, with the AST comparison in the PR description. Behaviour changes go in a separate PR so a regression can be attributed.
- Modules are named for what they hold, not for a class inside them. A file that stops matching its name gets renamed with `git mv`.
- Comments state facts and issue numbers; the reasoning goes in the PR.
