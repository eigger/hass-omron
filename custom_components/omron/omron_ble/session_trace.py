"""Where one BLE session spent its time and, if it failed, where it died.

A session is a chain of stages -- connect, service check, unlock, memory
session open, time sync, readout, close -- and a failure at each one means
something different: ``connect`` is usually a cuff that is asleep, ``unlock``
a key the cuff no longer accepts, ``readout`` a link that dropped mid-read.
The debug log has all of this, but nobody has debug logging on when a poll
fails at 3 am. The trace records the same breakdown as plain data so the
diagnostic sensors can publish it as attributes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from time import perf_counter
from typing import Any, TypeVar

_T = TypeVar("_T")


class SessionTrace:
    """Per-stage timings, scalar facts and the stage a failure happened in.

    ``timed(name)`` wraps one stage; it may nest, and the innermost stage an
    exception escapes from is what ``failed_stage`` records (the first one
    wins, so a close that also fails does not overwrite the real cause).
    ``note()`` attaches facts such as the number of connect attempts.
    """

    def __init__(self) -> None:
        self._timings: dict[str, float] = {}
        self._stack: list[str] = []
        self.failed_stage: str | None = None
        self.facts: dict[str, Any] = {}

    @property
    def stage(self) -> str | None:
        """The stage currently running, or None between stages."""
        return self._stack[-1] if self._stack else None

    @contextmanager
    def timed(self, name: str) -> Iterator[None]:
        """Time one stage; a repeated stage (a retried unlock) adds up."""
        self._stack.append(name)
        started = perf_counter()
        try:
            yield
        except BaseException:
            if self.failed_stage is None:
                self.failed_stage = name
            raise
        finally:
            self._stack.pop()
            self._timings[name] = round(
                self._timings.get(name, 0.0) + perf_counter() - started, 3
            )

    def fail(self, name: str) -> None:
        """Record a failure in a stage that reported it by return value.

        ``timed`` only sees exceptions; a check that returns False and leaves
        the raise to its caller (``verify_parent_service``) has to say so
        itself, or the failure is attributed to no stage at all.
        """
        if self.failed_stage is None:
            self.failed_stage = name

    def forgive(self, name: str | None = None) -> None:
        """Un-record a failure the caller went on to swallow.

        ``name`` limits it to that stage; None clears whichever stage it was
        (a retry loop that swallows anything its attempt raised).
        """
        if name is None or self.failed_stage == name:
            self.failed_stage = None

    def note(self, **facts: Any) -> None:
        """Record scalar facts about the session (None values are dropped)."""
        for key, value in facts.items():
            if value is not None:
                self.facts[key] = value

    def as_dict(self) -> dict[str, Any]:
        """The failed stage (if any), the facts, then each stage's seconds as
        ``<stage>_s`` in run order."""
        return {
            **({"failed_stage": self.failed_stage} if self.failed_stage else {}),
            **self.facts,
            **{f"{name}_s": seconds for name, seconds in self._timings.items()},
        }


def traced(
    stage: str,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Time a session method as one stage of ``self.trace``.

    A host without a trace (a bare transport stand-in) runs the method
    untimed rather than failing on the bookkeeping.
    """

    def decorate(method: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        @wraps(method)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> _T:
            trace = getattr(self, "trace", None)
            if trace is None:
                return await method(self, *args, **kwargs)
            with trace.timed(stage):
                return await method(self, *args, **kwargs)

        return wrapper

    return decorate
