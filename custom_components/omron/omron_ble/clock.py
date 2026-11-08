"""The clock the library stamps onto a cuff and reads cuff timestamps against.

A cuff stores wall-clock time with no zone. What it holds is only right if the
time written and the zone later attached to a stored record are the same zone,
so both come from here. The host sets the zone once with
``set_time_zone_provider``; without it the process zone is used, which keeps
the library usable on its own.
"""

from __future__ import annotations

from collections.abc import Callable
import datetime as dt

_provider: Callable[[], dt.tzinfo | None] | None = None


def set_time_zone_provider(provider: Callable[[], dt.tzinfo | None] | None) -> None:
    """Set where the zone comes from; ``None`` goes back to the process zone."""
    global _provider  # noqa: PLW0603
    _provider = provider


def local_tz() -> dt.tzinfo:
    """The configured zone, else the process zone."""
    tz = _provider() if _provider is not None else None
    return tz or dt.datetime.now().astimezone().tzinfo or dt.UTC


def local_now() -> dt.datetime:
    """Timezone-aware current time in the configured zone."""
    return dt.datetime.now(local_tz())


def local_wall_now() -> dt.datetime:
    """Current wall-clock time in the configured zone, without a zone attached.

    Naive on purpose: record timestamps parsed from a cuff are naive, and the
    driver compares the two.
    """
    return local_now().replace(tzinfo=None)
