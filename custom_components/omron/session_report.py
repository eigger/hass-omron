"""The breakdown of one BLE session, as the diagnostic sensors publish it.

``SessionTrace`` records where the session spent its time and where it died;
this adds what only Home Assistant knows -- which radio the link went over,
its signal, how many radios reach the cuff -- and one sentence on what the
failure most likely means, so a failed poll can be read off the entity
without debug logging.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.bluetooth import (
    BaseHaRemoteScanner,
    async_scanner_by_source,
    async_scanner_devices_by_address,
)
from homeassistant.core import HomeAssistant

# Below this the placement advice is worth giving; above it the radio is not
# the first suspect.
_WEAK_RSSI_DBM = -85
# The poll deadline (POLL_TIMEOUT_SECONDS) cancels the session with a bare
# TimeoutError; every other timeout on the way carries a message.
_DEADLINE_ERROR = "Session deadline reached; the BLE stack stopped answering"


def radio_facts(
    hass: HomeAssistant, address: str, link_info: dict[str, Any]
) -> dict[str, str | int]:
    """Which radio the session went through, its RSSI and how many reach the cuff.

    ``link_info`` is what the connect recorded: ``via`` is the path the link
    actually took (the backend's source), ``source`` the scanner that
    advertised it. Only the radio that paired holds the bond on a multi-proxy
    setup (#91), so the two are reported apart when they differ.
    """
    facts: dict[str, str | int] = {}
    advertised = _scanner(hass, link_info.get("source"))
    # A BlueZ link reports a D-Bus path rather than a scanner id, which is
    # the local adapter that advertised it.
    connected = _scanner(hass, link_info.get("via")) or advertised
    if connected is not None:
        facts["via"] = connected.name
        facts["via_type"] = "proxy" if isinstance(connected, BaseHaRemoteScanner) else "adapter"
    elif link_info.get("via"):
        facts["via"] = str(link_info["via"])
    # Only when they differ: the link took another radio than the one whose
    # advertisement was strongest -- a failover, or on a multi-proxy setup
    # the one that holds the bond (#91).
    if advertised is not None and advertised is not connected:
        facts["advertised_via"] = advertised.name
    rssi_scanner = connected or advertised
    if rssi_scanner is not None:
        try:
            seen = rssi_scanner.get_discovered_device_advertisement_data(address)
        except Exception:  # noqa: BLE001 - a scanner without the method
            seen = None
        if seen is not None:
            facts["rssi"] = seen[1].rssi
    # How many connectable radios currently see the cuff: 1 means no failover.
    facts["paths"] = len(async_scanner_devices_by_address(hass, address, connectable=True))
    return facts


def _scanner(hass: HomeAssistant, source: Any) -> Any:
    """The scanner behind a habluetooth source id, or None."""
    if not source:
        return None
    return async_scanner_by_source(hass, str(source))


def likely_cause(
    stage: str | None, error: str, facts: dict[str, Any], operation: str
) -> str:
    """One sentence on what a failed session most likely means.

    Read from where it died, the error text and the radio situation. Best
    effort -- ``error`` keeps the exact detail.
    """
    err = error.lower()
    placement = ""
    rssi = facts.get("rssi")
    if isinstance(rssi, int) and rssi <= _WEAK_RSSI_DBM:
        placement = f" The signal is weak ({rssi} dBm via {facts.get('via')})"
        if facts.get("paths") == 1:
            placement += " and no other radio reaches the cuff"
        placement += " — move the cuff or add a proxy."
    if error == _DEADLINE_ERROR:
        return (
            "The BLE stack stopped answering mid-session and the poll was cut at "
            "its deadline: usually a wedged adapter or a proxy that died. "
            "Restart the adapter / proxy if it repeats."
        )
    if stage == "connect":
        if "slot" in err:
            return (
                "The proxy has no free connection slot; add a proxy or reduce "
                "the BLE devices it serves."
            )
        if "settle" in err:
            return (
                "The cuff accepted the link and dropped it before encryption "
                "settled: on a multi-proxy setup usually a proxy that does not "
                "hold the bond (only the radio that paired does), otherwise a "
                "stale bond — pair again if it repeats."
            )
        return (
            "The BLE link could not be established. A cuff keeps its radio off "
            "between readings and wakes for a short window after a measurement, "
            "so this is its normal state; a sync that never succeeds is "
            f"different.{placement}"
        )
    if stage == "services":
        return (
            "Connected, but the cuff does not expose the service this model "
            "profile expects: the configured model may not match the cuff, or "
            "the proxy served a stale GATT cache."
        )
    if stage == "pair":
        if "programming" in err:
            return (
                "The cuff was not in pairing mode: it must show the blinking -P- "
                "while pairing runs."
            )
        return (
            "Bonding with the cuff failed: it was not in its pairing window, or "
            "it still holds a bond for another host (the phone app)."
        )
    if stage == "unlock":
        if "key mismatch" in err:
            return (
                "The cuff refused the stored pairing key: it has been paired to "
                "another host (the phone app) since, or the key is stale — pair "
                "again in -P- mode."
            )
        if "credential" in err:
            return (
                "No usable transport credential is stored for this cuff; re-add "
                "it while it shows -P-."
            )
        if "key missing" in err or "auth" in err or "0x06" in err:
            return (
                "The cuff no longer accepts the stored bond: it was paired "
                "elsewhere since, or the bond was lost (a re-flashed proxy, a "
                "changed adapter) — pair again."
            )
        return (
            "The cuff did not complete the unlock handshake: the link dropped or "
            f"the bond is stale — pair again if it repeats.{placement}"
        )
    if stage == "memory_open":
        return (
            "The cuff refused to start a readout session: usually a stale bond "
            "or a cuff that was paired to another host since — pair again if it "
            "repeats."
        )
    if stage == "readout":
        return (
            "The link dropped while reading the record memory. Once: link "
            "quality; every time at the same point: the memory map for this "
            f"model may be wrong — please open an issue.{placement}"
        )
    if stage in ("memory_close", "disconnect"):
        return (
            "The records were read; only the session close failed. Harmless "
            "unless the next connection is refused."
        )
    if stage == "time_sync":
        return (
            "Writing the clock failed; the records were not read on this "
            "session. Usually transient."
        )
    if operation == "pairing":
        return "Pairing did not complete; make sure the cuff shows -P- and retry."
    return "The session failed before the first stage was reached; see error."


def build_session_report(
    hass: HomeAssistant,
    address: str,
    *,
    operation: str,
    trace: dict[str, Any] | None,
    exc: BaseException | None,
) -> dict[str, Any]:
    """Assemble the attributes for one finished session.

    Outcome first, then the radio, then the trace (facts and stage timings),
    so the fields a reader looks at first are at the top of the attribute
    list.
    """
    trace = dict(trace or {})
    link_info = {key: trace.pop(key) for key in ("via", "source") if key in trace}
    facts = radio_facts(hass, address, link_info)
    report: dict[str, Any] = {"operation": operation, "success": exc is None}
    if exc is not None:
        error = str(exc) or type(exc).__name__
        if isinstance(exc, TimeoutError) and not str(exc):
            error = _DEADLINE_ERROR
        # The stage the failure escaped from. ``timed`` keeps the first one,
        # so a close that also failed after the real cause does not win.
        stage = trace.pop("failed_stage", None)
        report["error"] = error
        if stage is not None:
            report["failed_stage"] = stage
        report["likely_cause"] = likely_cause(stage, error, facts, operation)
    else:
        trace.pop("failed_stage", None)
    report.update(facts)
    report.update(trace)
    return report
