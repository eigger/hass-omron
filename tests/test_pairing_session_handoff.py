"""Pairing-session handoff regression tests (discussions#119).

WLD3.0 cuffs (HEM-7380T1 / 7188T1 and friends) serve data during the pairing
session and reject later connections — see the _WLD3_BOND_POLICY comment in
device_catalog.py. Closing the link after pairing and letting the follow-up
poll reconnect is therefore what makes that poll time out.

The config flow already parked its session in _setup_sessions so the first
poll reuses the same link, but the advertisement-triggered auto-pairing
(__init__.py) and the Retry Pairing button (button.py) dropped the session and
reconnected. These tests pin the missing wiring.

conftest replaces homeassistant/bleak with MagicMock, so the classes involved
cannot be instantiated (same constraint as test_poll_deadline.py). Pure
helpers are therefore exercised at runtime and the call-site wiring is checked
structurally with the AST.
"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

_COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / "omron"


def _parse(relative_path: str) -> ast.Module:
    return ast.parse((_COMPONENT / relative_path).read_text(encoding="utf-8"))


def _find_async_function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(
        f"{name} not found — update this test if it was renamed"
    )


def _find_method(
    tree: ast.AST, class_name: str, method_name: str
) -> ast.AsyncFunctionDef:
    """Find a method within a named class: the name alone is ambiguous here."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return _find_async_function(node, method_name)
    raise AssertionError(
        f"{class_name} not found — update this test if it was renamed"
    )


def _called_names(node: ast.AST) -> set[str]:
    return {
        ast.unparse(sub.func) for sub in ast.walk(node) if isinstance(sub, ast.Call)
    }


def _handoff_block(fn: ast.AST) -> ast.AsyncWith:
    """Return the `async with handed_off_session(...)` statement inside fn."""
    for node in ast.walk(fn):
        if isinstance(node, ast.AsyncWith) and any(
            isinstance(item.context_expr, ast.Call)
            and ast.unparse(item.context_expr.func) == "handed_off_session"
            for item in node.items
        ):
            return node
    raise AssertionError(
        "no `async with handed_off_session(...)` block — the pairing session "
        "is not being handed to the poll that follows"
    )


class _FakeSession:
    """Minimal session stub that records the ownership calls it receives."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def release_for_handoff(self) -> "_FakeSession":
        self.events.append("release")
        return self

    def reclaim_ownership(self) -> None:
        self.events.append("reclaim")

    async def aclose(self) -> None:
        self.events.append("aclose")


def _fake_hass() -> SimpleNamespace:
    return SimpleNamespace(data={})


class TestHandoffHelpers:
    """Ownership handling of the stash/discard helpers."""

    def test_stash_releases_ownership_and_parks_the_session(self):
        from custom_components.omron.ble_session import stash_handoff_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session)

        assert session.events == ["release"]
        assert hass.data[DOMAIN]["_setup_sessions"]["AA:BB:CC:DD:EE:FF"] is session

    def test_discard_reclaims_before_closing(self):
        """release_for_handoff() cleared the disconnect responsibility, so
        aclose() without reclaiming first would leave the link up."""
        from custom_components.omron.ble_session import (
            discard_handoff_session,
            stash_handoff_session,
        )
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()
        stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session)

        asyncio.run(discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF"))

        assert session.events == ["release", "reclaim", "aclose"]
        assert hass.data[DOMAIN]["_setup_sessions"] == {}

    def test_discard_is_a_noop_once_the_poll_adopted_the_session(self):
        """Cleanup after the poll popped the session must pass quietly."""
        from custom_components.omron.ble_session import discard_handoff_session

        asyncio.run(discard_handoff_session(_fake_hass(), "AA:BB:CC:DD:EE:FF"))

    def test_discard_swallows_close_errors(self):
        """A failed cleanup must not overturn a successful pairing."""
        from custom_components.omron.ble_session import (
            discard_handoff_session,
            stash_handoff_session,
        )

        class _ExplodingSession(_FakeSession):
            async def aclose(self) -> None:
                raise RuntimeError("link already gone")

        hass = _fake_hass()
        stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", _ExplodingSession())

        asyncio.run(discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF"))


class TestHandedOffSessionScope:
    """The context manager both call sites use."""

    def test_session_is_available_inside_and_closed_after(self):
        from custom_components.omron.ble_session import handed_off_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        async def _run():
            async with handed_off_session(hass, "AA:BB:CC:DD:EE:FF", session):
                parked = hass.data[DOMAIN]["_setup_sessions"]
                assert parked["AA:BB:CC:DD:EE:FF"] is session

        asyncio.run(_run())

        assert session.events == ["release", "reclaim", "aclose"]
        assert hass.data[DOMAIN]["_setup_sessions"] == {}

    def test_adopted_session_is_left_alone(self):
        """Once the poll pops the session it owns it; exiting must not close it."""
        from custom_components.omron.ble_session import handed_off_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        async def _run():
            async with handed_off_session(hass, "AA:BB:CC:DD:EE:FF", session):
                hass.data[DOMAIN]["_setup_sessions"].pop("AA:BB:CC:DD:EE:FF")

        asyncio.run(_run())

        assert session.events == ["release"]

    def test_session_is_closed_when_the_poll_raises(self):
        from custom_components.omron.ble_session import handed_off_session

        hass = _fake_hass()
        session = _FakeSession()

        async def _run():
            async with handed_off_session(hass, "AA:BB:CC:DD:EE:FF", session):
                raise RuntimeError("refresh blew up")

        with_error = False
        try:
            asyncio.run(_run())
        except RuntimeError:
            with_error = True

        assert with_error, "the error must propagate, not be swallowed"
        assert session.events == ["release", "reclaim", "aclose"]


class TestRetryPairingReturnsSession:
    """async_retry_pairing must hand the link over, not close it."""

    def test_returns_the_session_instead_of_none(self):
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_retry_pairing")

        assert fn.returns is not None and "OmronDeviceSession" in ast.unparse(
            fn.returns
        ), "async_retry_pairing must return the live session"

        returned = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Return) and node.value is not None
        ]
        assert returned, (
            "without a returned session the callers have no link to hand off, "
            "which puts the reconnect-after-pairing behaviour back"
        )
        assert not any(
            "release_for_handoff" in ast.unparse(node.value) for node in returned
        ), (
            "ownership is released in stash_handoff_session(); releasing here "
            "too means a caller that never parks the session is handed a link "
            "its aclose() will not close"
        )

    def test_leaves_the_memory_session_open_for_the_poll(self):
        """The readout session stays open so the poll can read on the same link."""
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_retry_pairing")

        keywords = [
            kw
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "leave_memory_session_open"
        ]
        assert keywords, "async_sync_device_time needs leave_memory_session_open"
        assert any(
            isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in keywords
        )

    def test_closes_the_session_when_pairing_fails(self):
        """A failed pairing must drop the link instead of leaking it."""
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_retry_pairing")

        handlers = [
            node for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler)
        ]
        closing = [h for h in handlers if "aclose" in ast.unparse(h)]
        assert closing, "the failure path must call session.aclose()"
        assert any(
            any(isinstance(sub, ast.Raise) for sub in ast.walk(h)) for h in closing
        ), "the original error must be re-raised after cleanup"


class TestCallSitesHandOffTheSession:
    """Every path that pairs must hand the session to the poll that follows."""

    def test_auto_pairing_hands_off_the_session(self):
        fn = _find_async_function(_parse("__init__.py"), "_run_auto_session")

        assert "handed_off_session" in _called_names(fn), (
            "dropping the session here makes the follow-up poll reconnect, and "
            "a PER_SESSION cuff refuses that second connect"
        )

    def test_retry_pairing_button_hands_off_the_session(self):
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )

        assert "handed_off_session" in _called_names(fn)

    def test_config_flow_uses_the_shared_helper(self):
        """All three paths share one helper so the wiring cannot drift apart."""
        fn = _find_async_function(_parse("config_flow.py"), "_async_do_pairing")

        assert "stash_handoff_session" in _called_names(fn)

    def test_poll_cleanup_reclaims_ownership_before_closing(self):
        """A handed-off session the poll never adopted only drops on reclaim."""
        fn = _find_async_function(_parse("__init__.py"), "_async_poll_data")

        assert any(
            name.endswith("reclaim_ownership") for name in _called_names(fn)
        ), "aclose() does not disconnect while _owns_connection is False"


class TestPostPairingPollIsNotDebounced:
    """The poll that adopts the parked session must actually run inside the block.

    async_request_refresh() goes through a 10 s debouncer. When a refresh
    fired recently it schedules the poll and returns *without* running it, so
    the handed_off_session block would exit and close the parked link before
    the deferred poll could adopt it — reproducing the very failure this
    handoff exists to prevent. Pressing Refresh Data and then Retry Pairing
    lands squarely in that window.
    """

    def _assert_direct_refresh(self, fn: ast.AST, where: str) -> None:
        block = _handoff_block(fn)
        calls = _called_names(block)
        debounced = {name for name in calls if name.endswith("async_request_refresh")}

        assert not debounced, (
            f"{where}: async_request_refresh() is debounced and can return "
            "before the poll runs, so the block would close the parked "
            "session first. Use async_refresh(), as setup does."
        )
        assert any(name.endswith("async_refresh") for name in calls), (
            f"{where}: the parked session is only useful if a poll actually "
            "runs inside the block"
        )

    def test_auto_pairing_polls_without_the_debouncer(self):
        fn = _find_async_function(_parse("__init__.py"), "_run_auto_session")
        self._assert_direct_refresh(fn, "auto-pairing")

    def test_retry_pairing_button_polls_without_the_debouncer(self):
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )
        self._assert_direct_refresh(fn, "Retry Pairing button")


class TestSkippedPollKeepsTheSessionParked:
    """Bailing out before connecting must not consume the parked session."""

    def test_session_is_adopted_only_inside_the_lock(self):
        """Adopting at the top of the poll would hand the bail-out paths (no
        device, session lock held) a link they close without ever using."""
        fn = _find_async_function(_parse("__init__.py"), "_async_poll_data")

        adopting = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "adopt_handoff_session"
        ]
        assert adopting, "the poll must take the parked session explicitly"

        locked_blocks = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.AsyncWith)
            and any("session_lock" in ast.unparse(item.context_expr) for item in node.items)
        ]
        assert locked_blocks, "expected the poll to run under session_lock"

        inside = {
            id(call)
            for block in locked_blocks
            for call in ast.walk(block)
            if isinstance(call, ast.Call)
        }
        assert all(id(call) in inside for call in adopting), (
            "adopt_handoff_session() runs before the bail-out guards, so a "
            "skipped poll closes the pairing link instead of leaving it for "
            "the retry"
        )

    def test_stale_handed_off_session_is_closed_not_leaked(self):
        """A parked link that dropped must be closed before connecting fresh."""
        fn = _find_async_function(_parse("omron_ble/parser.py"), "async_poll")

        adoption = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If)
            and "preconnected_session" in ast.unparse(node.test)
            and "is_connected" in ast.unparse(node.test)
        ]
        assert adoption, "expected the is_connected guard around adoption"

        fallback = "\n".join(ast.unparse(stmt) for stmt in adoption[0].orelse)
        assert "aclose" in fallback, (
            "declining a stale handed-off session without closing it leaves "
            "the link half-open while the poll opens a second connection"
        )
        assert "reclaim_ownership" in fallback, (
            "aclose() does not disconnect while _owns_connection is False"
        )
