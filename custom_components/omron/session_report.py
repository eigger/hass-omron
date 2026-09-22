"""The breakdown of one BLE session, as the diagnostic sensors publish it.

``SessionTrace`` records where the session spent its time and where it died.
``blesession`` turns that into the shared report — which radio the link went
over, and one sentence on what a failure most likely means. The sentences
that are specific to a cuff live here; the ones every BLE device shares
(a proxy with no free slot, a weak signal, a stack that stopped answering)
come from the library.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from blesession import build_report, cause_key, placement, stages
from blesession.hass import radio_facts

from .omron_ble.session_trace import SessionTrace

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

# The poll deadline (POLL_TIMEOUT_SECONDS) cancels the session with a bare
# TimeoutError; every other timeout on the way carries a message.
_DEADLINE_ERROR = "Session deadline reached; the BLE stack stopped answering"


# When neither table has anything to say. Every other sentence names a stage.
_NO_STAGE_CAUSE = "The session failed before the first stage was reached; see error."


def cuff_cause(
    stage: str | None,
    detail: str | None,
    error: str,
    facts: Mapping[str, Any],
    operation: str,
) -> str | None:
    """The cuff's own reading of a failure, or None where the shared one is it.

    ``stage`` is the shared name (``auth``, ``transfer``, …) and ``detail``
    the cuff's own (``unlock``, ``readout``, …). Best effort — ``error``
    keeps the exact detail. A ``None`` leaves the sentence to
    ``blesession``, which words the failures every BLE device shares and
    hands the report a ``likely_cause_key`` for the one it chose.
    """
    err = error.lower()
    where = detail or stage
    advice = placement(facts, noun="cuff")
    if error == _DEADLINE_ERROR:
        return (
            "The BLE stack stopped answering mid-session and the poll was cut at "
            "its deadline: usually a wedged adapter or a proxy that died. "
            "Restart the adapter / proxy if it repeats."
        )
    if where == "settle":
        return (
            "The cuff accepted the link and dropped it before encryption "
            "settled: on a multi-proxy setup usually a proxy that does not "
            "hold the bond (only the radio that paired does), otherwise a "
            "stale bond — pair again if it repeats."
        )
    if where == stages.CONNECT:
        # "slot" is the same on every proxy; the shared sentence covers it.
        if "slot" in err:
            return None
        return (
            "The BLE link could not be established. A cuff keeps its radio off "
            "between readings and wakes for a short window after a measurement, "
            "so this is its normal state; a sync that never succeeds is "
            f"different.{advice}"
        )
    if where == "services":
        return (
            "Connected, but the cuff does not expose the service this model "
            "profile expects: the configured model may not match the cuff, or "
            "the proxy served a stale GATT cache."
        )
    if where == "pair":
        if "programming" in err:
            return (
                "The cuff was not in pairing mode: it must show the blinking -P- "
                "while pairing runs."
            )
        return (
            "Bonding with the cuff failed: it was not in its pairing window, or "
            "it still holds a bond for another host (the phone app)."
        )
    if where == "unlock":
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
            f"the bond is stale — pair again if it repeats.{advice}"
        )
    if where == "memory_open":
        return (
            "The cuff refused to start a readout session: usually a stale bond "
            "or a cuff that was paired to another host since — pair again if it "
            "repeats."
        )
    if where == "readout":
        return (
            "The link dropped while reading the record memory. Once: link "
            "quality; every time at the same point: the memory map for this "
            f"model may be wrong — please open an issue.{advice}"
        )
    if where in ("memory_close", stages.DISCONNECT):
        return (
            "The records were read; only the session close failed. Harmless "
            "unless the next connection is refused."
        )
    if where == "time_sync":
        return (
            "Writing the clock failed; the records were not read on this "
            "session. Usually transient."
        )
    if operation == "pairing" and where is None:
        return "Pairing did not complete; make sure the cuff shows -P- and retry."
    return None


def build_session_report(
    hass: HomeAssistant,
    address: str,
    *,
    operation: str,
    trace: SessionTrace | None,
    exc: BaseException | None,
) -> dict[str, Any]:
    """Assemble the attributes for one finished session.

    Outcome first, then the radio, then the stage timings and the facts the
    session noted, in the order ``blesession.build_report`` fixes so the same
    keys mean the same thing on every integration.
    """
    trace = trace if trace is not None else SessionTrace()
    if isinstance(exc, TimeoutError) and not str(exc):
        exc = TimeoutError(_DEADLINE_ERROR)

    def cause(
        stage: str | None, detail: str | None, error: str, facts: Mapping[str, Any]
    ) -> str | None:
        """The cuff's sentence; ``None`` hands the failure back to the library.

        Handing it back rather than calling ``generic_cause`` here is what
        puts ``likely_cause_key`` on the report — the stable name for the
        shared sentence, which a translation can key on — and lets the
        library read the exception type, not just the message text. The
        last-resort sentence is only for a failure neither table names, so
        it is returned only once ``cause_key`` says the library has none.
        """
        text = cuff_cause(stage, detail, error, facts, operation)
        if text is not None:
            return text
        if cause_key(stage, error, exc=exc) is not None:
            return None
        return _NO_STAGE_CAUSE

    return build_report(
        operation=operation,
        trace=trace,
        exc=exc,
        facts=radio_facts(hass, address, trace.link),
        cause=cause,
        noun="cuff",
    )
