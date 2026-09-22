"""Where one Omron BLE session spent its time, and where it died.

The trace is :class:`blesession.SessionTrace`. This module only names the
cuff's own stages and which shared stage each one is, so a failure report
can say both (``failed_stage: auth``, ``failed_detail: unlock``).
``connect`` and ``disconnect`` are already shared names.
``time_sync``, ``device_info`` and ``registration`` are not: a failure there
is forgiven or noted, and an unmapped name is reported as itself.
"""

from __future__ import annotations

from blesession import SessionTrace as _SessionTrace
from blesession import stages
from blesession import traced as traced

# Device stage -> the shared stage the report and the generic sentences use.
STAGE_MAP: dict[str, str] = {
    "services": stages.SESSION,
    "pair": stages.AUTH,
    "unlock": stages.AUTH,
    "memory_open": stages.AUTH,
    "readout": stages.TRANSFER,
    "memory_close": stages.FINISH,
}


class SessionTrace(_SessionTrace):
    """A session trace that already knows the cuff's stage names."""

    def __init__(self) -> None:
        super().__init__(STAGE_MAP)


__all__ = ["STAGE_MAP", "SessionTrace", "traced"]
