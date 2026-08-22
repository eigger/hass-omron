"""앱 레이어 본드 키 영속화 회귀 테스트.

실기기 btsnoop(이슈 #92, HEM-7188T1-LEO)이 보여주는 공식 앱의 동작:

    세션 1 (페어링 모드): 0x11/0x91 → 0x70 01 → 0xf0 81 → 0x70 05 → 0xf0 85
                                    → 0x70 06 → 0xf0 86 → 암호화된 0xc0 데이터
    세션 2 (재연결):      0x11/0x91 →           0x70 05 → 0xf0 85
                                    → 0x70 06 → 0xf0 86 → 암호화된 0xc0 데이터

두 번째 세션에 ``0x70 01`` 이 없다. 페어링이 만들어낸 키를 들고 있으니 다시
페어링할 이유가 없고, 애초에 -P- 모드가 아닌 커프는 그 요청을 받아주지 않는다.

``SecureSession`` 은 처음부터 ``stored_ltk`` 로 재연결 모드를 지원했지만, 키를
어디에도 저장하지 않아 매 세션이 ``is_bonding=True`` 로 시작했다 — 즉 항상
페어링을 요청했고, -P- 창 밖에서는 ``0xff26`` 을 받았다. 그게 "이 기기는 ECDH
를 거부한다" 로 읽혔다.
"""
import asyncio

import pytest

from custom_components.omron.omron_ble.secure_store import (
    VALID_KEY_LENGTHS,
    SecureBondStore,
)
from custom_components.omron.omron_ble.secure_session import SecureSession


LTK = bytes(range(16))


# ── 저장소 ────────────────────────────────────────────────────────────────

def test_store_starts_empty_so_the_first_session_pairs():
    assert SecureBondStore().load() is None


def test_store_round_trips_a_key():
    store = SecureBondStore()
    asyncio.run(store.save(LTK))
    assert store.load() == LTK


def test_store_clears_so_a_rejected_key_cannot_lock_us_out():
    """거부된 키를 계속 들고 있으면 영영 페어링으로 못 돌아간다.

    페어링은 키가 없을 때만 일어나므로, 나쁜 키는 그것을 고칠 수 있는 유일한
    세션을 막아버린다.
    """
    store = SecureBondStore(LTK)
    asyncio.run(store.clear())
    assert store.load() is None


@pytest.mark.parametrize("length", [0, 8, 15, 17, 79, 81])
def test_store_rejects_keys_of_the_wrong_size(length):
    """잘못된 길이를 저장하면 재연결 때 조용히 깨진다 — 그때는 원인이 안 보인다."""
    with pytest.raises(ValueError):
        asyncio.run(SecureBondStore().save(bytes(length)))


@pytest.mark.parametrize("length", VALID_KEY_LENGTHS)
def test_store_accepts_both_key_forms(length):
    """16B LTK 와 일부 펌웨어가 주는 80B 확장 토큰 둘 다."""
    store = SecureBondStore()
    asyncio.run(store.save(bytes(length)))
    assert store.load() == bytes(length)


# ── 세션 모드 ─────────────────────────────────────────────────────────────

def test_no_stored_key_means_pairing_mode():
    assert SecureSession().is_bonding is True


def test_stored_key_means_reconnect_mode():
    session = SecureSession(stored_ltk=LTK)

    assert session.is_bonding is False
    assert session.ltk == LTK


def test_reconnect_mode_refuses_to_pair():
    """재연결 모드는 ``0x70 01`` 을 거부해야 한다 — 그게 이 변경의 핵심이다.

    ``build_pair_req`` 는 모드를 보기 전에 cryptography 를 먼저 요구하므로,
    그 패키지가 없는 환경에서는 이 단언을 할 수 없다(HA 에는 기본 포함).
    """
    pytest.importorskip("cryptography")
    session = SecureSession(stored_ltk=LTK)

    with pytest.raises(RuntimeError, match="reconnect mode"):
        session.build_pair_req()


def test_reconnect_opens_at_0x7005_with_the_stored_key():
    """저장된 키로 시작하는 세션은 곧바로 Start Encryption 을 만든다.

    캡처의 두 번째 세션이 정확히 이 프레임으로 열린다.
    """
    pytest.importorskip("cryptography")
    session = SecureSession(stored_ltk=LTK)

    frame = session.build_start_enc_req()

    assert frame[:2] == b"\x70\x05"
    assert len(frame) == 46          # 캡처된 0x7005 프레임과 같은 길이
    assert session.ltk == LTK, "재연결이 저장된 키를 새로 유도해서는 안 된다"


def test_pairing_derives_a_storable_key():
    """페어링 요청은 캡처와 같은 89바이트 레이아웃이어야 한다."""
    pytest.importorskip("cryptography")
    session = SecureSession()

    req = session.build_pair_req()

    assert req[:2] == b"\x70\x01"
    assert len(req) == 89            # 2 + salt(7) + challenge(16) + pubkey(64)


# ── 배선 ──────────────────────────────────────────────────────────────────

def test_session_and_parser_hand_the_store_down():
    """키는 링크보다 오래 살아야 한다 — 세션마다 새로 만들면 의미가 없다."""
    from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession

    store = SecureBondStore(LTK)
    session = OmronDeviceSession(object(), _config(), store)
    assert session._secure_bond_store is store

    # 파서는 conftest 가 MagicMock 베이스로 대체해서 인스턴스 속성을 확인할 수
    # 없으므로 배선 자체를 본다. 이 줄이 빠지면 폴마다 새 키로 시작한다.
    parser = _parser_source()
    assert "secure_bond_store or SecureBondStore()" in parser
    # 대입 1회 + 세션 생성 3곳.
    assert parser.count("self._secure_bond_store") == 4, (
        "세 곳의 세션 생성 모두가 저장소를 넘겨야 한다"
    )
    assert "secure_bond_store or SecureBondStore()" in _driver_source()


def test_driver_only_pairs_when_there_is_no_stored_key():
    """재연결에서 ``0x70 01`` 을 보내면 -P- 창 밖이라 거부당한다."""
    source = _driver_source()
    body = source[source.index("async def _secure_unlock") :]
    body = body[: body.index("\n    async def _discard_secure_bond")]

    guard = body.index("if not reconnecting:")
    assert guard < body.index("build_pair_req()"), (
        "페어링 요청이 재연결 가드 밖에 있다"
    )
    assert "SecureSession(stored_ltk=stored_key)" in body


def test_driver_stores_the_key_pairing_produced():
    source = _driver_source()
    body = source[source.index("async def _secure_unlock") :]
    body = body[: body.index("\n    async def _discard_secure_bond")]

    save = body.index("self._secure_bond_store.save(derived)")
    # 나머지 단계가 실패해도 키는 남아야 한다: 커프는 -P- 를 다시 누르기
    # 전까지 같은 페어링을 두 번 내주지 않는다.
    assert save < body.index("# Step 2: Send Encryption Start Request")


def test_driver_discards_a_key_the_cuff_refused():
    source = _driver_source()
    body = source[source.index("async def _secure_unlock") :]
    body = body[: body.index("\n    async def _discard_secure_bond")]

    # 정확히 한 곳에서만, 그리고 실제로 거부당했을 때만 버린다.
    assert body.count("await self._discard_secure_bond(") == 1
    assert "if reconnecting and key_was_rejected:" in body


def test_a_blip_does_not_cost_the_key():
    """-P- 를 눌러 얻은 키를 BLE 한 번 흔들림으로 지우면 안 된다.

    ``_secure_unlock`` 은 토큰 핸드셰이크로 시작한다. 재연결 중 거기서
    타임아웃이 나거나 링크가 끊겼다는 이유로 키를 버리면, 사용자는 기기까지
    다시 가야 한다. 같은 키로 다음 폴에서 재시도하면 되는 실패다.
    """
    source = _driver_source()
    body = source[source.index("async def _secure_unlock") :]
    body = body[: body.index("\n    async def _discard_secure_bond")]

    # 커프가 secure 단계를 에러 프레임으로 거부했을 때만 세워진다.
    assert body.count("key_was_rejected = True") == 2, (
        "encryption start 와 challenge 거부 두 곳에서만 세워야 한다"
    )
    timeout = body.index("except asyncio.TimeoutError")
    generic = body.index("except Exception as exc:")
    discard = body.index("await self._discard_secure_bond(")
    assert timeout < generic < discard, "타임아웃 경로는 키를 건드리지 않아야 한다"


def test_secure_failure_falls_back_to_the_token_path():
    """새로 배선한 경로가 실패해도 오늘 되던 읽기까지 잃지는 않는다."""
    source = _driver_source()
    body = source[source.index("async def unlock(self, key") :]
    body = body[: body.index("\n    async def ")]

    assert "if not self._config.token_key_fallback:" in body
    assert "raise" in body
    assert "await self._token_unlock()" in body


def _config():
    from custom_components.omron.omron_ble.devices import get_device_config

    return get_device_config("HEM-7188T1")


def _driver_source():
    import pathlib

    return pathlib.Path(
        "custom_components/omron/omron_ble/omron_driver.py"
    ).read_text()


def _parser_source():
    import pathlib

    return pathlib.Path("custom_components/omron/omron_ble/parser.py").read_text()


# ── 리뷰 반영: 저장이 통합을 리로드하면 안 된다 ────────────────────────────

def test_saving_the_key_does_not_touch_the_config_entry():
    """엔트리를 고치면 update_listener 가 통합을 리로드한다.

    ``save()`` 는 핸드셰이크 도중에 불린다. 거기서 리로드가 걸리면 방금
    페어링한 그 세션이 뜯긴다. 그래서 키는 HA 자체 저장소로 간다.
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    body = source[source.index("class PersistentSecureBondStore") :]
    body = body[: body.index("\ndef _merge_poll_sensor_update")]

    assert "async_update_entry" not in body, (
        "키 저장이 엔트리를 고치면 세션 도중 리로드가 걸린다"
    )
    assert "self._store.async_save" in body
    # update_listener 는 여전히 무조건 리로드한다 — 그래서 피해야 하는 것이다.
    listener = source[source.index("async def update_listener") :][:300]
    assert "async_reload" in listener


def test_the_config_flow_key_is_adopted_once_then_lives_in_storage():
    """엔트리는 첫 키가 놓이는 자리일 뿐이다 — 저장할 entry_id 가 없어서."""
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    body = source[source.index("class PersistentSecureBondStore") :]
    body = body[: body.index("\ndef _merge_poll_sensor_update")]

    load = body.index("async def async_load")
    assert body.index("self._entry.data.get(CONF_SECURE_BOND_KEY)") > load
    # 읽고 나서 엔트리에서 지우지 않는다: 지우는 것도 엔트리 수정이다.
    assert "data.pop(CONF_SECURE_BOND_KEY" not in body


def test_setup_loads_the_key_before_the_first_session():
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    assert "await secure_bond_store.async_load()" in source
    assert source.index("await secure_bond_store.async_load()") < source.index(
        "secure_bond_store=secure_bond_store,"
    )


def test_adopted_sessions_keep_the_store():
    """``adopt()`` 가 빈 저장소를 만들면 그 세션은 페어링 요청으로 시작한다."""
    import pathlib

    driver = pathlib.Path(
        "custom_components/omron/omron_ble/omron_driver.py"
    ).read_text()
    time_sync = pathlib.Path(
        "custom_components/omron/omron_ble/setup_time_sync.py"
    ).read_text()

    assert "secure_bond_store: SecureBondStore | None = None," in driver
    assert "session._secure_bond_store = secure_bond_store or SecureBondStore()" in driver
    # setup_time_sync 는 transport 가 없을 때 직접 adopt 한다.
    assert "OmronDeviceSession.adopt(client, config, secure_bond_store)" in time_sync
