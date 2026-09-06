# Experimental X2+ session probe

Hardware-tested on HEM-7188T1-LEO with macOS/CoreBluetooth and Linux/BlueZ.
This is not enabled by the Home Assistant device catalog and does not claim
support for other OMRON models or Bluetooth proxies.

Use an isolated Python environment with the integration's Python dependencies,
including cryptography, bleak, bleak-retry-connector, sensor-state-data and
aiooui. On Linux the passive pairing agent additionally requires dbus-fast and
access to the system BlueZ service. Run `python examples/x2_session_probe.py
--help` for required arguments. Use the repository root as `--checkout`.

## Explicit first pairing

The owner must activate physical pairing mode. The successful Linux test began
with no existing X2 association on that test host, using both
`--no-connect-pair --passive-bonding-agent`. The latter retains the upstream
BlueZ agent without explicitly calling Pair. The probe never removes an OS
association automatically. Removing one is a separate owner-controlled action;
do not remove unrelated Bluetooth devices.

```sh
python examples/x2_session_probe.py pair \
  --checkout /path/to/hass-omron \
  --credentials /private/path/x2-key.json \
  --address YOUR_DEVICE_ADDRESS \
  --no-connect-pair --passive-bonding-agent
```

For macOS omit `--passive-bonding-agent` and use the CoreBluetooth device UUID.
First pairing writes preserved initialization settings and the host's local
clock. Check the host clock/timezone first. It may replace the meter's existing
application association. It does not read measurement records.

The credential is created privately, without overwriting an existing file,
only after the initialization close is accepted. It is bound to the device
address and host name. Do not commit it, attach it to an issue, or copy a phone's
credentials into this test. Keep it for reconnects; a transient failure is not
permission to erase it or fall back to fresh pairing.

## Reconnect after display-off

Let the display turn off and do not reactivate P. Start a separate process:

```sh
python examples/x2_session_probe.py reconnect \
  --checkout /path/to/hass-omron \
  --credentials /private/path/x2-key.json \
  --address YOUR_DEVICE_ADDRESS --no-connect-pair
```

The hardware-validated result is `ADVERTISEMENT_MODE normal`,
`X2_RESUME_AUTH_OK`, `METADATA_READ_OK bytes=24; NO_RECORDS_READ`,
`READ_CLOSE_OK`, `CONNECTION_CLOSED`, exit 0. Reconnect does not initialize the
clock or write settings. A display-off reconnect is not a battery-removal test.

## Remaining Home Assistant integration work

- In config_flow, choose the exact X2 profile without forcing connect-time
  Pair; persist the returned application credential only after accepted close.
- Use a dedicated credential field and bind it to the device and adapter route.
  The existing bindkey field is not yet consumed by the poll path for this flow.
- Pass that credential through setup/parser into the session authentication
  path; currently `_secure_unlock` constructs a fresh SecureSession each time.
- Keep explicit pairing initialization separate from ordinary polling; do not
  run generic pairing/time initialization again during saved-key resume.
- Integrate and test failure recovery, handoff-session ownership, credential
  redaction in diagnostics, HA restart and adapter-route changes before enabling
  automatic catalog selection. No proxy acceptance is implied by local BlueZ.

The successful Linux run did not have an SMP capture. Deferred pairing plus a
fresh host association and retained agent is a tested combination, not proof
that explicit Pair timing alone caused earlier FF26 failures.
