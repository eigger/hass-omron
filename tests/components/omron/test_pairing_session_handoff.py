"""Pairing-session handoff regression tests (discussions#119).

WLD3.0 cuffs (HEM-7380T1 / 7188T1 and friends) serve data during the pairing
session and reject later connections — see the _WLD3_BOND_POLICY comment in
device_catalog.py. Closing the link after pairing and letting the follow-up
poll reconnect is therefore what makes that poll time out.

The config flow already parked its session in _setup_sessions so the first
poll reuses the same link, but the advertisement-triggered auto-pairing
(__init__.py) and the Retry Pairing button (button.py) dropped the session and
reconnected. These tests pin the missing wiring.

The helpers are exercised directly with a stand-in session; every call site
(retry-pairing button, advertisement auto-session, config flow, poll, unload)
runs inside a real Home Assistant with a loaded config entry, replacing only
the BLE device boundary.
"""

import asyncio
import contextlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bleak.backends.device import BLEDevice
from homeassistant.config_entries import SOURCE_BLUETOOTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

import custom_components.omron.config_flow as config_flow
from custom_components.omron import async_poll_data, process_service_info
from custom_components.omron.const import CONF_DEVICE_MODEL, DOMAIN
from custom_components.omron.omron_ble.const import OMRON_MANUFACTURER_ID
from custom_components.omron.session_handoff import stash_handoff_session

from bt import service_info


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
        from custom_components.omron.session_handoff import stash_handoff_session
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        asyncio.run(stash_handoff_session(hass, "AA:BB:CC:DD:EE:FF", session))

        assert session.events == ["release"]
        assert hass.data[DOMAIN]["_setup_sessions"]["AA:BB:CC:DD:EE:FF"] is session

    def test_discard_reclaims_before_closing(self):
        """release_for_handoff() cleared the disconnect responsibility, so
        aclose() without reclaiming first would leave the link up."""
        from custom_components.omron.session_handoff import (
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
        from custom_components.omron.session_handoff import discard_handoff_session

        asyncio.run(discard_handoff_session(_fake_hass(), "AA:BB:CC:DD:EE:FF"))

    def test_discard_swallows_close_errors(self):
        """A failed cleanup must not overturn a successful pairing."""
        from custom_components.omron.session_handoff import (
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
        from custom_components.omron.session_handoff import run_post_pairing_poll
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()
        seen = {}

        class _Coordinator:
            async def async_refresh(self) -> None:
                seen["parked"] = hass.data[DOMAIN]["_setup_sessions"].get(
                    TestPostPairingPoll.ADDRESS
                )

        asyncio.run(run_post_pairing_poll(hass, self.ADDRESS, session, _Coordinator()))

        assert seen["parked"] is session, "the poll must see the parked link"
        assert session.events == ["release"]

    def test_cancelled_post_pairing_poll_discards_its_parked_session(self):
        from custom_components.omron.session_handoff import run_post_pairing_poll
        from custom_components.omron.const import DOMAIN

        hass = _fake_hass()
        session = _FakeSession()

        class _CancelledCoordinator:
            async def async_refresh(self) -> None:
                raise asyncio.CancelledError

        async def scenario():
            try:
                await run_post_pairing_poll(
                    hass, self.ADDRESS, session, _CancelledCoordinator()
                )
            except asyncio.CancelledError:
                return
            raise AssertionError("cancellation should propagate")

        asyncio.run(scenario())
        assert session.events == ["release", "reclaim", "aclose"]
        assert hass.data[DOMAIN]["_setup_sessions"] == {}

    def test_skipped_poll_leaves_the_session_parked(self):
        """The poll can bail out before adopting — no device, or the BLE
        session lock taken by an advertisement-triggered auto-session. Closing
        the link here would send the retry back to the reconnect a PER_SESSION
        cuff refuses, so it has to stay parked for the next poll."""
        from custom_components.omron.session_handoff import run_post_pairing_poll
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
        from custom_components.omron.session_handoff import run_post_pairing_poll
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
        from custom_components.omron.session_handoff import poll_parked_session

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
        from custom_components.omron.session_handoff import (
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
        from custom_components.omron.session_handoff import stash_handoff_session
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
        from custom_components.omron.session_handoff import stash_handoff_session
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
        from custom_components.omron.session_handoff import (
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


ADDRESS = "AA:BB:CC:DD:EE:FF"
# Format 0x03 status byte: 0x04 = invalid time, 0x08 = pairing mode.
_INVALID_TIME = bytes([0x03, 0x04, 0x00, 0x00, 0x00])
_PAIRING_MODE = bytes([0x03, 0x08, 0x00, 0x00, 0x00])


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="HEM-7155T EEFF",
        data={CONF_DEVICE_MODEL: "HEM-7155T"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # Let the setup-time poll finish so the task set holds only what a test starts.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    async with asyncio.timeout(5):
        while entry.runtime_data.background_tasks:
            await asyncio.sleep(0.01)
    return entry


def _parked(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get("_setup_sessions", {})


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


def _advertise(entry: MockConfigEntry, payload: bytes) -> None:
    entry.runtime_data.last_attempt_time = 0.0  # past the cooldown
    process_service_info(
        entry,
        service_info(
            ADDRESS,
            name="HEM-7155T",
            manufacturer_data={OMRON_MANUFACTURER_ID: payload},
        ),
    )


async def _advertisement_session_done(entry: MockConfigEntry) -> None:
    tasks = set(entry.runtime_data.background_tasks)
    assert tasks, "the advertisement did not start a session task"
    async with asyncio.timeout(5):
        await asyncio.wait(tasks)


class _Probe:
    """Stands in for the poll coordinator's refresh and records what it saw."""

    def __init__(self, hass: HomeAssistant, runtime) -> None:
        self.hass = hass
        self.runtime = runtime
        self.calls = 0
        self.parked_at_poll = None
        self.lock_held_at_poll = None

    async def __call__(self) -> None:
        self.calls += 1
        self.parked_at_poll = _parked(self.hass).get(ADDRESS)
        self.lock_held_at_poll = self.runtime.session_lock.locked()


@contextlib.contextmanager
def _ble_boundary(hass: HomeAssistant, entry: MockConfigEntry, *, paired=None):
    """Replace the radio: a visible device, a pairing that yields ``paired``."""
    runtime = entry.runtime_data
    device = BLEDevice(ADDRESS, "HEM-7155T", {})
    probe = _Probe(hass, runtime)
    pairing = AsyncMock(return_value=paired)
    sync_time = AsyncMock()
    with (
        patch("custom_components.omron.SETTLE_DELAY_SECONDS", 0),
        patch(
            "custom_components.omron.button.async_ble_device_from_address",
            return_value=device,
        ),
        patch.object(runtime.poll_coordinator, "async_refresh", probe),
        patch.object(runtime.device_data, "async_retry_pairing", pairing),
        patch.object(runtime.device_data, "async_sync_time", sync_time),
    ):
        yield SimpleNamespace(probe=probe, pairing=pairing, sync_time=sync_time)


async def _press_retry_pairing(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": "button.hem_7155t_eeff_retry_pairing"},
        blocking=True,
    )


class TestRepairEntryPointsCheckForAParkedSession:
    """Both paths that pair must consult the parked session first."""

    async def test_retry_pairing_button_polls_the_parked_link_instead_of_pairing(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        entry = await _setup(hass)
        parked = _FakeSession()
        await stash_handoff_session(hass, ADDRESS, parked)

        with _ble_boundary(hass, entry) as ble:
            await _press_retry_pairing(hass)

        assert not ble.pairing.await_count, (
            "pressing Retry Pairing while a link is parked would open a second "
            "BLE connection to the same cuff"
        )
        assert ble.probe.calls == 1
        # The poll it triggers needs session_lock to adopt the parked session,
        # so the check has to run outside the lock.
        assert ble.probe.lock_held_at_poll is False
        await _unload(hass, entry)

    @pytest.mark.parametrize(
        ("payload", "device_call"),
        [(_PAIRING_MODE, "pairing"), (_INVALID_TIME, "sync_time")],
        ids=["auto_pairing", "time_sync"],
    )
    async def test_advertisement_session_polls_the_parked_link_instead(
        self,
        hass: HomeAssistant,
        enable_bluetooth: None,
        payload: bytes,
        device_call: str,
    ) -> None:
        """async_sync_time opens its own link too, so the time-sync advert must
        not put a second one on the cuff just as pairing would."""
        entry = await _setup(hass)
        runtime = entry.runtime_data
        await stash_handoff_session(hass, ADDRESS, _FakeSession())

        with _ble_boundary(hass, entry) as ble:
            _advertise(entry, payload)
            await _advertisement_session_done(entry)

        assert getattr(ble, device_call).await_count == 0
        assert ble.probe.calls == 1
        assert ble.probe.lock_held_at_poll is False
        # Bailing out seeds the cooldown, or every advertisement in a burst
        # would respawn the session task.
        assert runtime.last_attempt_time > 0.0
        await _unload(hass, entry)


class TestRetryPairingReturnsSession:
    """async_retry_pairing must hand the link over, not close it."""

    @staticmethod
    def _parser_and_session(session):
        from custom_components.omron.omron_ble import OmronBluetoothDeviceData

        data = OmronBluetoothDeviceData(device_model="HEM-7155T")
        data._open_session = lambda ble_device, pairing_session=False: session
        return data

    @staticmethod
    def _pairing_session(**overrides):
        from custom_components.omron.omron_ble.session_trace import SessionTrace

        events: list[str] = []

        class _Session:
            trace = SessionTrace()
            client = object()
            new_credential = None

            async def connect(self):
                events.append("connect")

            async def verify_parent_service(self):
                return True

            async def pair(self):
                events.append("pair")

            async def refresh_services(self):
                events.append("refresh_services")

            async def aclose(self):
                events.append("aclose")

            def publish_link_to(self, trace):
                pass

        session = _Session()
        for name, value in overrides.items():
            setattr(session, name, value)
        return session, events

    async def test_returns_the_open_session_with_the_memory_session_left_open(
        self,
    ) -> None:
        session, events = self._pairing_session()
        data = self._parser_and_session(session)
        ble_device = BLEDevice(ADDRESS, "HEM-7155T", {})

        with patch(
            "custom_components.omron.omron_ble.parser.async_sync_device_time",
            AsyncMock(),
        ) as sync:
            returned = await data.async_retry_pairing(ble_device)

        assert returned is session, "the callers have no link to hand off otherwise"
        assert "aclose" not in events
        assert sync.await_args.kwargs["leave_memory_session_open"] is True

    async def test_ownership_is_released_by_the_parking_helper_not_here(self) -> None:
        released = []
        session, _ = self._pairing_session(
            release_for_handoff=lambda: released.append(True)
        )
        data = self._parser_and_session(session)

        with patch(
            "custom_components.omron.omron_ble.parser.async_sync_device_time",
            AsyncMock(),
        ):
            await data.async_retry_pairing(BLEDevice(ADDRESS, "HEM-7155T", {}))

        assert not released, (
            "a caller that never parks the session would be handed a link its "
            "aclose() will not close"
        )

    async def test_closes_the_session_and_reraises_when_pairing_fails(self) -> None:
        async def refuse():
            raise ConnectionError("bond refused")

        session, events = self._pairing_session(pair=refuse)
        data = self._parser_and_session(session)

        with pytest.raises(ConnectionError, match="bond refused"):
            await data.async_retry_pairing(BLEDevice(ADDRESS, "HEM-7155T", {}))

        assert events[-1] == "aclose"


class TestCallSitesHandOffTheSession:
    """Every path that pairs must hand the session to the poll that follows."""

    async def test_auto_pairing_hands_off_the_session(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        entry = await _setup(hass)
        paired = _FakeSession()

        with _ble_boundary(hass, entry, paired=paired) as ble:
            _advertise(entry, _PAIRING_MODE)
            await _advertisement_session_done(entry)

        assert ble.probe.calls == 1
        assert ble.probe.parked_at_poll is paired, (
            "dropping the session here makes the follow-up poll reconnect, and "
            "a PER_SESSION cuff refuses that second connect"
        )
        assert paired.events == ["release"]
        _parked(hass).pop(ADDRESS, None)
        await _unload(hass, entry)

    async def test_retry_pairing_button_hands_off_the_session_and_seeds_cooldown(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        entry = await _setup(hass)
        runtime = entry.runtime_data
        runtime.last_attempt_time = 0.0
        paired = _FakeSession()

        with _ble_boundary(hass, entry, paired=paired) as ble:
            await _press_retry_pairing(hass)

        assert ble.probe.calls == 1
        assert ble.probe.parked_at_poll is paired
        assert ble.probe.lock_held_at_poll is False
        # Without the seed an advert could start an auto-session that takes the
        # lock between pairing and the poll, so the poll would skip.
        assert runtime.last_attempt_time > 0.0
        _parked(hass).pop(ADDRESS, None)
        await _unload(hass, entry)

    async def test_config_flow_parks_the_session_through_the_shared_helper(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        paired = _FakeSession()

        class _FlowSession(_FakeSession):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__()
                self.new_credential = None

            async def connect(self) -> None:
                self.events.append("connect")

            def __new__(cls, *args, **kwargs):
                return paired

        paired.new_credential = None
        paired.connect = AsyncMock()
        with (
            patch.object(config_flow, "OmronDeviceSession", _FlowSession),
            patch.object(config_flow, "async_pair_and_sync_device", AsyncMock()),
            patch.object(
                config_flow,
                "async_ble_device_from_address",
                return_value=BLEDevice(ADDRESS, "HEM-7155T", {}),
            ),
            patch("custom_components.omron.async_poll_data", AsyncMock()),
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_BLUETOOTH},
                data=service_info(
                    ADDRESS,
                    name="HEM-7155T",
                    manufacturer_data={OMRON_MANUFACTURER_ID: bytes([0x01, 0, 0, 0])},
                ),
            )
            while result["type"] is FlowResultType.FORM:
                step = result["step_id"]
                user_input = (
                    {CONF_DEVICE_MODEL: "HEM-7155T"} if step == ("select_model") else {}
                )
                if step == "user_aliases":
                    user_input = {}
                result = await hass.config_entries.flow.async_configure(
                    result["flow_id"], user_input=user_input
                )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            await hass.async_block_till_done()

            assert paired.events[:2] == ["connect", "release"] or (
                "release" in paired.events
            )
            assert "release" in paired.events, "ownership is released when parking"
            assert _parked(hass).get(ADDRESS) is paired

            _parked(hass).pop(ADDRESS, None)
            (entry,) = hass.config_entries.async_entries(DOMAIN)
            await hass.config_entries.async_unload(entry.entry_id)

    async def test_poll_cleanup_reclaims_ownership_before_closing(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        """A handed-off session the poll never adopted only drops on reclaim."""
        entry = await _setup(hass)
        parked = _FakeSession()
        await stash_handoff_session(hass, ADDRESS, parked)

        @contextlib.asynccontextmanager
        async def _refuses(*args, **kwargs):
            raise RuntimeError("telemetry unavailable")
            yield

        with (
            patch(
                "custom_components.omron.async_ble_device_from_address",
                return_value=BLEDevice(ADDRESS, "HEM-7155T", {}),
            ),
            patch("custom_components.omron.omron_poll_ble_telemetry", _refuses),
        ):
            with pytest.raises(UpdateFailed):
                await async_poll_data(hass, entry)

        # aclose() does not disconnect while ownership is released.
        assert parked.events == ["release", "reclaim", "aclose"]
        assert ADDRESS not in _parked(hass)
        await _unload(hass, entry)

    async def test_unload_clears_a_parked_session(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        """Nothing adopts or closes a parked link once the entry is gone."""
        entry = await _setup(hass)
        parked = _FakeSession()
        await stash_handoff_session(hass, ADDRESS, parked)

        await _unload(hass, entry)

        assert parked.events == ["release", "reclaim", "aclose"]
        assert ADDRESS not in _parked(hass)


class TestPostPairingPollIsNotDebounced:
    """The post-pairing poll must actually run before the helper returns.

    async_request_refresh() goes through a 10 s debouncer: when a refresh
    fired recently it schedules the poll and returns *without* running it.
    Pressing Refresh Data and then Retry Pairing lands squarely in that
    window.
    """

    def test_helper_polls_without_the_debouncer(self):
        from custom_components.omron.session_handoff import run_post_pairing_poll

        ran = []

        class _DebouncedCoordinator:
            async def async_request_refresh(self) -> None:
                return  # scheduled for later, nothing ran

            async def async_refresh(self) -> None:
                ran.append(True)

        asyncio.run(
            run_post_pairing_poll(
                _fake_hass(),
                "AA:BB:CC:DD:EE:FF",
                _FakeSession(),
                _DebouncedCoordinator(),
            )
        )

        assert ran, "the poll must run now, not be left to a debouncer"


class TestSkippedPollKeepsTheSessionParked:
    """Bailing out before connecting must not consume the parked session."""

    @pytest.mark.parametrize("skip", ["no_device", "lock_held"])
    async def test_skipped_scheduled_poll_leaves_the_link_parked(
        self, hass: HomeAssistant, enable_bluetooth: None, skip: str
    ) -> None:
        """Adopting at the top of the poll would hand the bail-out paths a link
        they close without ever using."""
        entry = await _setup(hass)
        runtime = entry.runtime_data
        parked = _FakeSession()
        await stash_handoff_session(hass, ADDRESS, parked)
        device = BLEDevice(ADDRESS, "HEM-7155T", {})
        poll = AsyncMock()

        with (
            patch(
                "custom_components.omron.async_ble_device_from_address",
                return_value=None if skip == "no_device" else device,
            ),
            patch.object(runtime.device_data, "async_poll", poll),
        ):
            if skip == "lock_held":
                await runtime.session_lock.acquire()
            try:
                await async_poll_data(hass, entry)
            finally:
                if skip == "lock_held":
                    runtime.session_lock.release()

        poll.assert_not_awaited()
        assert _parked(hass).get(ADDRESS) is parked
        assert "aclose" not in parked.events
        await _unload(hass, entry)

    async def test_poll_adopts_the_parked_link_once_it_holds_the_lock(
        self, hass: HomeAssistant, enable_bluetooth: None
    ) -> None:
        entry = await _setup(hass)
        parked = _FakeSession()
        await stash_handoff_session(hass, ADDRESS, parked)
        poll = AsyncMock(return_value=entry.runtime_data.device_data._finish_update())

        with (
            patch(
                "custom_components.omron.async_ble_device_from_address",
                return_value=BLEDevice(ADDRESS, "HEM-7155T", {}),
            ),
            patch.object(entry.runtime_data.device_data, "async_poll", poll),
        ):
            await async_poll_data(hass, entry)

        assert poll.await_args.kwargs["preconnected_session"] is parked
        assert ADDRESS not in _parked(hass)
        await _unload(hass, entry)

    async def test_stale_handed_off_session_is_closed_not_leaked(self) -> None:
        """A parked link that dropped must be closed before connecting fresh."""
        from custom_components.omron.omron_ble import OmronBluetoothDeviceData

        class _Stop(Exception):
            pass

        class _Fresh:
            trace = None

            async def __aenter__(self):
                raise _Stop

            async def __aexit__(self, *exc_info) -> bool:
                return False

            def publish_link_to(self, trace) -> None:
                return None

        dropped = _FakeSession(connected=False)
        dropped._pairing_session = False
        opened = []
        data = OmronBluetoothDeviceData(device_model="HEM-7155T")

        def _open(ble_device, pairing_session=False):
            opened.append(pairing_session)
            return _Fresh()

        data._open_session = _open

        with pytest.raises(_Stop):
            await data.async_poll(
                BLEDevice(ADDRESS, "HEM-7155T", {}), preconnected_session=dropped
            )

        # aclose() does not disconnect while ownership is released.
        assert dropped.events == ["reclaim", "aclose"]
        assert opened == [False], "a fresh connection replaces the dropped link"
