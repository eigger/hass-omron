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



class _FakeSession:
    """Minimal session stub that records the ownership calls it receives."""

    def __init__(self, connected: bool = True) -> None:
        self.events: list[str] = []
        self.is_connected = connected

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

        asyncio.run(stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session))

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

        async def _run():
            await stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session)
            await discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF")

        asyncio.run(_run())

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

        async def _run():
            await stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", _ExplodingSession())
            await discard_handoff_session(hass, "AA:BB:CC:DD:EE:FF")

        asyncio.run(_run())


class TestPostPairingPoll:
    """run_post_pairing_poll: park the link, then actually run a poll on it."""

    ADDRESS = "AA:BB:CC:DD:EE:FF"

    def test_session_is_parked_before_the_poll_runs(self):
        from custom_components.omron.ble_session import run_post_pairing_poll
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()
        seen = {}

        class _Coordinator:
            async def async_refresh(self) -> None:
                seen["parked"] = hass.data[DOMAIN]["_setup_sessions"].get(
                    TestPostPairingPoll.ADDRESS
                )

        asyncio.run(
            run_post_pairing_poll(hass, self.ADDRESS, session, _Coordinator())
        )

        assert seen["parked"] is session, "the poll must see the parked link"
        assert session.events == ["release"]

    def test_skipped_poll_leaves_the_session_parked(self):
        """The poll can bail out before adopting — no device, or the BLE
        session lock taken by an advertisement-triggered auto-session. Closing
        the link here would send the retry back to the reconnect a PER_SESSION
        cuff refuses, so it has to stay parked for the next poll."""
        from custom_components.omron.ble_session import run_post_pairing_poll
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        class _SkippingCoordinator:
            async def async_refresh(self) -> None:
                return  # bailed out before adopting anything

        asyncio.run(
            run_post_pairing_poll(hass, self.ADDRESS, session, _SkippingCoordinator())
        )

        parked = hass.data[DOMAIN]["_setup_sessions"].get(self.ADDRESS)
        assert parked is session, "a skipped poll must not consume the session"
        assert "aclose" not in session.events, "the link must stay open for the retry"

    def test_adopted_session_is_left_to_the_poll(self):
        """Once the poll pops the session it owns it; nothing here may close it."""
        from custom_components.omron.ble_session import run_post_pairing_poll
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        class _AdoptingCoordinator:
            async def async_refresh(self) -> None:
                hass.data[DOMAIN]["_setup_sessions"].pop(TestPostPairingPoll.ADDRESS)

        asyncio.run(
            run_post_pairing_poll(hass, self.ADDRESS, session, _AdoptingCoordinator())
        )

        assert session.events == ["release"]


class TestParkedSessionBlocksRepairing:
    """A parked link is still connected — pairing again would make it two.

    A skipped poll leaves its session parked and open. That is precisely when
    a user presses Retry Pairing again (no data showed up), and when the
    advertisement auto-pairing fires after its 60 s cooldown. Connecting again
    would put a second BLE link on the same cuff, which is the SMP auth
    failure this integration serializes against, and would replace the parked
    session without closing it.
    """

    ADDRESS = "AA:BB:CC:DD:EE:FF"

    def test_reports_nothing_parked_so_pairing_proceeds(self):
        from custom_components.omron.ble_session import poll_parked_session

        polled = False

        class _Coordinator:
            async def async_refresh(self) -> None:
                nonlocal polled
                polled = True

        handled = asyncio.run(
            poll_parked_session(_fake_hass(), self.ADDRESS, _Coordinator())
        )

        assert handled is False, "with nothing parked the caller must pair"
        assert not polled

    def test_polls_the_parked_session_instead_of_pairing(self):
        from custom_components.omron.ble_session import (
            poll_parked_session,
            stash_handoff_session,
        )

        hass = _fake_hass()
        session = _FakeSession()
        polled = False

        class _Coordinator:
            async def async_refresh(self) -> None:
                nonlocal polled
                polled = True

        async def _run():
            await stash_handoff_session(hass, self.ADDRESS, session)
            return await poll_parked_session(hass, self.ADDRESS, _Coordinator())

        handled = asyncio.run(_run())

        assert handled is True, "the caller must skip pairing, not open a second link"
        assert polled, "the parked link has to be polled, not just left alone"

    def test_replacing_a_parked_session_closes_the_old_link(self):
        """Backstop for a caller that pairs anyway: the overwritten session
        would otherwise be dropped with nothing left to close it."""
        from custom_components.omron.ble_session import stash_handoff_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        first, second = _FakeSession(), _FakeSession()

        async def _run():
            await stash_handoff_session(hass, self.ADDRESS, first)
            await stash_handoff_session(hass, self.ADDRESS, second)

        asyncio.run(_run())

        assert first.events == ["release", "reclaim", "aclose"]
        assert hass.data[DOMAIN]["_setup_sessions"][self.ADDRESS] is second

    def test_restashing_the_same_session_does_not_close_it(self):
        from custom_components.omron.ble_session import stash_handoff_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        async def _run():
            await stash_handoff_session(hass, self.ADDRESS, session)
            await stash_handoff_session(hass, self.ADDRESS, session)

        asyncio.run(_run())

        assert "aclose" not in session.events
        assert hass.data[DOMAIN]["_setup_sessions"][self.ADDRESS] is session


class TestParkedSessionMustStillBeConnected:
    """A dropped parked link must not stand in for pairing."""

    ADDRESS = "AA:BB:CC:DD:EE:FF"

    def test_dropped_parked_session_is_discarded_so_pairing_proceeds(self):
        """Polling a dead parked link only reconnects without pairing, which
        is the connect a PER_SESSION cuff refuses — and the caller pressed
        Retry Pairing precisely because it wanted to pair."""
        from custom_components.omron.ble_session import (
            poll_parked_session,
            stash_handoff_session,
        )
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession(connected=False)
        polled = False

        class _Coordinator:
            async def async_refresh(self) -> None:
                nonlocal polled
                polled = True

        async def _run():
            await stash_handoff_session(hass, self.ADDRESS, session)
            return await poll_parked_session(hass, self.ADDRESS, _Coordinator())

        handled = asyncio.run(_run())

        assert handled is False, "a dead parked link must not block pairing"
        assert not polled, "there is nothing to poll on a dropped link"
        assert session.events == ["release", "reclaim", "aclose"]
        assert hass.data[DOMAIN]["_setup_sessions"] == {}


class TestRepairEntryPointsCheckForAParkedSession:
    """Both paths that pair must consult the parked session first."""

    def test_retry_pairing_button_checks_before_pairing(self):
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )
        body = ast.unparse(fn)

        assert "poll_parked_session" in body, (
            "pressing Retry Pairing while a link is parked would open a second "
            "BLE connection to the same cuff"
        )
        assert body.index("poll_parked_session") < body.index("async_retry_pairing"), (
            "the check has to come before pairing, not after"
        )

    def test_auto_pairing_checks_before_pairing(self):
        fn = _find_async_function(_parse("__init__.py"), "_run_auto_session")
        body = ast.unparse(fn)

        assert "poll_parked_session" in body
        assert body.index("poll_parked_session") < body.index("async_retry_pairing")

    def test_check_also_covers_the_time_sync_path(self):
        """async_sync_time opens its own link, so an invalid_time advert would
        put a second one on the cuff just as pairing would."""
        fn = _find_async_function(_parse("__init__.py"), "_run_auto_session")
        body = ast.unparse(fn)

        assert body.index("poll_parked_session") < body.index("async_sync_time"), (
            "the guard must run before the time-sync branch too"
        )
        guards = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If)
            and "poll_parked_session" in ast.unparse(node.test)
        ]
        assert guards, "expected the parked-session guard to be a plain if"
        assert "is_pairing" not in ast.unparse(guards[0].test), (
            "gating on is_pairing leaves the time-sync path unguarded"
        )

    def test_auto_pairing_seeds_the_cooldown_when_it_bails_out(self):
        """Returning early without seeding it respawns this task on every
        advertisement in a burst."""
        fn = _find_async_function(_parse("__init__.py"), "_run_auto_session")

        guards = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If)
            and "poll_parked_session" in ast.unparse(node.test)
        ]
        assert "last_attempt_time" in ast.unparse(guards[0])

    def test_check_runs_outside_the_session_lock(self):
        """The poll it triggers needs session_lock to adopt the parked
        session, so holding the lock here would make that poll skip."""
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )

        locked_blocks = [
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.AsyncWith)
            and any("session_lock" in ast.unparse(item.context_expr) for item in node.items)
        ]
        assert locked_blocks, "expected pairing to run under session_lock"

        inside = {
            ast.unparse(call.func)
            for block in locked_blocks
            for call in ast.walk(block)
            if isinstance(call, ast.Call)
        }
        assert "poll_parked_session" not in inside


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

        assert "run_post_pairing_poll" in _called_names(fn), (
            "dropping the session here makes the follow-up poll reconnect, and "
            "a PER_SESSION cuff refuses that second connect"
        )

    def test_retry_pairing_button_hands_off_the_session(self):
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )

        assert "run_post_pairing_poll" in _called_names(fn)

    def test_retry_pairing_button_seeds_the_advert_cooldown(self):
        """Without this an advert can start an auto-session that takes the BLE
        lock between pairing and the poll, so the poll skips and the fresh
        link goes unused. Setup seeds the same cooldown for the same reason."""
        fn = _find_method(
            _parse("button.py"), "OmronRetryPairingButtonEntity", "async_press"
        )

        assert "last_attempt_time" in ast.unparse(fn)

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

    def test_unload_clears_a_parked_session(self):
        """Nothing adopts or closes a parked link once the entry is gone."""
        fn = _find_async_function(_parse("__init__.py"), "async_unload_entry")

        assert "discard_handoff_session" in _called_names(fn), (
            "a session parked for a poll that never came would keep its BLE "
            "link past the unload"
        )


class TestPostPairingPollIsNotDebounced:
    """The post-pairing poll must actually run before the helper returns.

    async_request_refresh() goes through a 10 s debouncer: when a refresh
    fired recently it schedules the poll and returns *without* running it.
    Pressing Refresh Data and then Retry Pairing lands squarely in that
    window. The forced-transfer path in _run_auto_session deliberately uses
    the debounced call, so this invariant is pinned on the shared helper
    rather than on the call sites.
    """

    def test_helper_polls_without_the_debouncer(self):
        fn = _find_async_function(_parse("ble_session.py"), "run_post_pairing_poll")
        calls = _called_names(fn)

        assert not any(
            name.endswith("async_request_refresh") for name in calls
        ), (
            "async_request_refresh() can return before the poll runs, leaving "
            "the parked session unused. Use async_refresh(), as setup does."
        )
        assert any(name.endswith("async_refresh") for name in calls), (
            "parking the session is only useful if a poll actually runs"
        )

    def test_helper_does_not_discard_the_session(self):
        """A poll that bailed out leaves the session parked for the next one;
        discarding here would close a link the retry still needs."""
        fn = _find_async_function(_parse("ble_session.py"), "run_post_pairing_poll")

        assert "discard_handoff_session" not in _called_names(fn)


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
