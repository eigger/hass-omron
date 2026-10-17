"""폴 데드라인 회귀 테스트 (hass-omron#110).

bleak 의 BlueZ 백엔드는 read/write/notify D-Bus 호출에 타임아웃을 걸지 않는다
(경계가 있는 건 disconnect 뿐이다). 그래서 bluetoothd 가 물리면 폴 하나가
영원히 대기하고, 그 폴이 session_lock 을 쥔 채로 남아 이후의 모든 예약 폴 /
Refresh Data / 광고 트리거가 "lock held" 로 조용히 빠져나간다. 로그도 안 남고
HA 재시작 전까지 통합이 죽는다.

이를 막는 두 불변조건을 실제 코드를 돌려 고정한다:

1. async_poll_data 는 async_poll 을 asyncio.timeout 으로 감싼다
   (``test_entry_lifecycle_behavior.py`` 의 ``test_poll_has_a_deadline_*`` 가
   실제 설정 항목으로 검증한다).
2. async_poll 의 예외 핸들러는 CancelledError 를 삼키지 않는다. asyncio.timeout
   은 마감 시각에 태스크를 딱 한 번만 취소하므로, 그 CancelledError 를 삼키면
   루프가 다음 무경계 대기로 넘어가고 다시 발동할 데드라인이 남지 않는다.
"""

from __future__ import annotations

import asyncio

from poll_fakes import (
    FakeSession,
    Hang,
    ble_device,
    parser_with_session,
    raising,
)


def test_poll_timeout_constant_is_positive():
    from custom_components.omron import POLL_TIMEOUT_SECONDS

    assert POLL_TIMEOUT_SECONDS > 0


async def _cancel_and_expect_it_to_escape(data, hang: Hang) -> None:
    task = asyncio.create_task(data.async_poll(ble_device()))
    async with asyncio.timeout(5):
        await hang.started.wait()
    task.cancel()
    done, _pending = await asyncio.wait([task], timeout=2)
    if not done:
        hang.release()
        task.cancel()
        await asyncio.wait([task], timeout=1)
        raise AssertionError(
            f"async_poll swallowed the deadline's cancellation and went on to "
            f"another wait ({hang.starts} starts)"
        )
    assert task.cancelled()


class TestCancellationEscapesPoll:
    """데드라인이 보낸 취소는 async_poll 밖으로 빠져나가야 한다."""

    async def test_during_the_service_check(self):
        hang = Hang()
        session = FakeSession(has_service=hang)
        await _cancel_and_expect_it_to_escape(parser_with_session(session), hang)

    async def test_during_a_memory_session_open_attempt(self, monkeypatch):
        """The retry loop must not take the cancellation for a failed attempt."""
        from custom_components.omron.omron_ble import parser as parser_module

        real_sleep = asyncio.sleep

        async def fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(parser_module.asyncio, "sleep", fast_sleep)
        hang = Hang()
        session = FakeSession(unlock_steps=[hang])
        await _cancel_and_expect_it_to_escape(parser_with_session(session), hang)
        assert session.unlock_calls == 1

    async def test_during_the_fallback_readout(self, monkeypatch):
        from custom_components.omron.omron_ble import parser as parser_module

        real_sleep = asyncio.sleep

        async def fast_sleep(_delay):
            await real_sleep(0)

        monkeypatch.setattr(parser_module.asyncio, "sleep", fast_sleep)
        hang = Hang()
        steps = [raising(OSError("open failed"))] * 3 + [hang]
        session = FakeSession(unlock_steps=steps)
        await _cancel_and_expect_it_to_escape(parser_with_session(session), hang)
        assert session.unlock_calls == 4
