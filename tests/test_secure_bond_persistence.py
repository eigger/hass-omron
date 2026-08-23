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


def test_driver_stores_the_key_only_once_the_handshake_completes():
    """핸드셰이크가 끝난 뒤에 저장해야 한다.

    초안은 키 교환 직후에 저장했다. "뒤에서 실패해도 -P- 를 낭비하지 말자"는
    의도였는데, 실기기 로그(이슈 #92)가 반대를 보여준다: 키를 받고 0x70 06 에서
    깨진 실행은 커프가 커밋하지 않은 LTK 를 우리만 들고 있게 만들었고, 다음
    세션이 재연결로 열려 거부당하고 키를 버렸다 — -P- 는 결국 낭비됐다.
    커프는 0xf0 86 에서 커밋하는 것으로 보인다.
    """
    body = _method_body(_driver_source(), "_secure_unlock")

    save = body.index("self._secure_bond_store.save(derived)")
    assert save > body.index("process_challenge_resp(challenge_resp)")
    # 재연결에는 저장할 새 키가 없다.
    assert "if not reconnecting:" in body[body.index("self._unlocked = True") : save]


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
    # 타임아웃 핸들러 블록만 떼어내 그 안에 폐기가 없는지 본다. 인덱스 대소
    # 비교는 try 안에 다른 except 가 생기면 엉뚱한 것을 집는다.
    timeout_at = body.index("        except asyncio.TimeoutError")
    tail = body[timeout_at:]
    timeout_block = tail[: tail.index("\n        except Exception as exc:")]
    assert "_discard_secure_bond" not in timeout_block, (
        "타임아웃 경로가 키를 버린다"
    )


def test_secure_failure_falls_back_to_the_token_path():
    """새로 배선한 경로가 실패해도 오늘 되던 읽기까지 잃지는 않는다."""
    source = _driver_source()
    body = source[source.index("async def unlock(self, key") :]
    body = body[: body.index("\n    async def ")]

    assert "if not self._config.token_key_fallback:" in body
    assert "raise" in body
    assert "await self._token_unlock()" in body



def _method_body(source: str, name: str) -> str:
    """``source`` 에서 메서드 하나의 본문만 잘라낸다.

    문자열 인덱스로 대충 자르면 다음 메서드까지 딸려 들어와 단언이 엉뚱한
    코드를 보게 된다.
    """
    import re

    start = re.search(rf"\n    (?:async )?def {name}\(", source)
    assert start, name
    rest = source[start.end() :]
    end = re.search(r"\n    (?:async )?def ", rest)
    return rest[: end.start()] if end else rest

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


def test_a_cleared_key_stays_cleared_across_a_restart():
    """엔트리에 남은 첫 키가 재시작 때 되살아나면 안 된다.

    ``clear()`` 는 스토어에 ``{"key": None}`` 을 쓴다. 그걸 "저장된 게 없음"
    으로 읽고 엔트리로 폴백하면, 커프가 거부해서 버린 키를 다시 집어온다 —
    같은 거부가 영원히 반복되고, -P- 로 다시 페어링해도 고쳐지지 않은 것처럼
    보인다. 스토어 파일이 아예 없을 때만 엔트리를 본다.
    """
    import pathlib

    source = pathlib.Path("custom_components/omron/__init__.py").read_text()
    body = source[source.index("class PersistentSecureBondStore") :]
    body = body[: body.index("\ndef _merge_poll_sensor_update")]

    load = body[body.index("async def async_load") :]
    load = load[: load.index("async def _adopt")]

    # dict 를 받았으면(=명시적으로 지웠어도) 거기서 끝난다.
    dict_branch = load.index("if isinstance(data, dict):")
    entry_fallback = load.index("self._entry.data.get(CONF_SECURE_BOND_KEY)")
    assert dict_branch < entry_fallback
    assert "return" in load[dict_branch:entry_fallback], (
        "스토어가 답했는데도 엔트리로 넘어가면 지운 키가 되살아난다"
    )
    # 엔트리 키는 한 번 흡수하면서 스토어에 써 둔다.
    assert "write_through=True" in load


def test_secure_family_subscribes_the_unlock_cccd_first():
    """CCCD 순서가 계열마다 다르다 — 폰 btsnoop 두 개가 정반대다.

        HEM-7188T1-LEO (#92):  0x001c(언락, 28) → 0x0021(RX, 33)
        HEM-7155T-ESLI (#67):  0x0021(RX, 33)   → 0x001c(언락, 28)

    2.5.23 은 두 계열 모두에 두 번째 순서를 썼고, 그 로그는 "CCD descriptor
    33" → "28" 직후 페어링 요청이 0xff26 으로 거부되는 것을 보여준다 —
    config flow 안에서, 커프가 페어링 모드일 때. 이 모듈은 CCCD 조작이 바로
    그 프레임을 유발할 수 있다는 것을 이미 알고 있었다(_secure_unlock 주석).
    """
    import pathlib

    source = pathlib.Path(
        "custom_components/omron/omron_ble/omron_driver.py"
    ).read_text()
    body = source[source.index("async def _token_unlock") :]
    body = body[: body.index("\n    async def ")]

    assert "if self._config.unlock_mode == UnlockMode.SECURE_SESSION:" in body
    branch = body.index("if self._config.unlock_mode == UnlockMode.SECURE_SESSION:")
    secure, other = body[branch:].split("else:", 1)
    # secure 계열: 언락 먼저
    assert secure.index("_subscribe_unlock()") < secure.index("_prime_rx()")
    # 나머지: RX 먼저 (7155T-ESLI/7142T2 캡처 그대로)
    assert other.index("_prime_rx()") < other.index("_subscribe_unlock()")


# ── 폴백이 세션을 무너뜨리면 안 된다 ───────────────────────────────────────

def test_a_half_built_session_does_not_encrypt():
    """실패한 핸드셰이크가 남긴 객체로 암호화하면 예외가 난다.

    실기기 로그(이슈 #92, 2.8.0-beta.1): secure 가 거부돼 평문으로 폴백하고
    토큰 언락까지 성공했는데, 그 다음 명령이 남아 있던 세션으로 암호화를
    시도해 ``encrypt is only valid in PAIRED state`` 로 세션 전체가 죽었다.
    폴백이 지키려던 그 읽기가 크래시로 바뀐 것이다.
    """
    pytest.importorskip("cryptography")
    session = SecureSession()

    assert session.is_encrypting is False       # IDLE
    session.build_pair_req()
    assert session.is_encrypting is False       # PAIR_REQ_SENT — 아직 아니다


def test_driver_asks_the_state_not_the_object():
    """``is not None`` 은 준비됐다는 뜻이 아니다."""
    source = _driver_source()

    assert source.count("if self._secure_frames_active():") == 2, (
        "암복호화 두 곳 모두 상태를 물어야 한다"
    )
    assert "session is not None and session.is_encrypting" in source
    assert "self._secure_session is not None:" not in source


def test_fallback_drops_the_failed_session():
    """폴백 전에 세션을 버려야 프레임이 평문으로 나간다."""
    source = _driver_source()
    body = source[source.index("async def unlock(self, key") :]
    body = body[: body.index("\n    async def ")]

    drop = body.index("self._secure_session = None")
    assert drop < body.index("await self._token_unlock()")


def test_successful_handshake_keeps_its_subscriptions():
    """성공한 핸드셰이크의 구독을 끄면 암호화 세션이 귀머거리가 된다.

    캡처에서 앱은 구독을 한 번도 놓지 않고, 암호화 0xc0 프레임을 바로 그
    언락 특성(handle 0x001b)에 쓴다. 성공 경로에서 stop_notify 하면 방금
    언락한 세션이 응답을 못 듣는다. 실패 경로는 종전대로 정리한다.
    """
    body = _method_body(_driver_source(), "_secure_unlock")

    finally_at = body.rindex("finally:")
    tail = body[finally_at:]
    assert "if self._unlocked:" in tail, "성공/실패를 갈라야 한다"
    success, failure = tail.split("else:", 1)
    # 성공 경로는 아무것도 내리지 않는다.
    assert "stop_notify" not in success
    assert "_release_primed_rx" not in success
    # 실패 경로는 언락과 RX 를 모두 내린다 — RX 는 기록까지 지우는 헬퍼로.
    assert "await self._client.stop_notify(self._config.unlock_uuid)" in failure
    assert "_release_primed_rx(" in failure


def test_challenge_request_logs_what_the_capture_cannot():
    """133 은 캡처가 설명하지 못한다 — 우리 쪽 값만이라도 남긴다.

    캡처는 0xf0 85 와 0x70 06 사이에 12ms 말고 아무것도 없고, ATT opcode 도
    우리와 같은 Write Request 다. 그래서 추측을 코드에 넣는 대신 경과 시간과
    링크 상태, 실제 연결 경로를 로그로 남긴다.
    """
    body = _method_body(_driver_source(), "_secure_unlock")

    log = body.index("Sending Challenge Request")
    after = body[log:]
    # 경과는 write 가 실제로 끝나거나 실패한 뒤에 찍혀야 한다 — 요청을 만드는
    # 데 걸린 시간만 재면 캡처의 12ms 와 비교할 값이 아니다.
    assert "ms after the" in after
    assert after.index("write_gatt_char") < after.index("ms after the")
    assert "is_connected=%s" in after
    assert "_connected_path(" in after
    # 시작점은 0xf0 85 를 받은 지점이어야 한다.
    assert body.index("enc_resp_at = time.monotonic()") < body.index(
        "build_challenge_req(enc_resp)"
    )
    # 캡처에 없는 동작은 넣지 않는다.
    start = body.index("build_challenge_req(enc_resp)")
    between = body[start : body.index("challenge_req, response=True", start)]
    assert "start_notify" not in between
    assert "asyncio.sleep" not in between


def test_the_driver_module_has_no_undefined_names():
    """소스 문자열만 보는 테스트는 NameError 를 못 잡는다.

    챌린지 로그를 넣으면서 다른 브랜치에만 있던 헬퍼를 참조했는데, 이 파일의
    단언들은 "그 이름이 소스에 있다" 만 확인하므로 전부 통과했다. 잡아낸 것은
    CI 의 lint 였다.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "custom_components/"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 127 or "No module named" in result.stderr:
        pytest.skip("ruff not installed")
    assert result.returncode == 0, result.stdout + result.stderr


# ── RX 디스패처 ───────────────────────────────────────────────────────────

def test_rx_prime_installs_a_dispatcher_not_a_throwaway_lambda():
    """더미 람다로 구독해두면 실제 콜백을 붙일 방법이 재구독뿐이다.

    그리고 재구독은 ``_start_notify_with_recovery`` 를 타는데, 그건
    "already enabled" 를 보면 CCCD 를 내렸다 다시 올린다 — 성공한 핸드셰이크가
    일부러 유지한 그 구독을, 첫 암호화 프레임 직전에.
    """
    driver = _driver_source()

    # 프라임은 두 곳에 있다 — _token_unlock 과 unlock() 의 클래식 경로.
    assert "lambda _h, _d: None" not in driver, "프라임이 아직 더미 람다다"
    assert driver.count(
        "self._config.rx_channel_uuids[0], self._rx_dispatch"
    ) == 2, "두 프라임 지점 모두 디스패처를 써야 한다"
    assert "def _rx_dispatch" in driver
    assert "handler = self._rx_notify_handler" in driver


def test_memory_session_swaps_the_handler_instead_of_resubscribing():
    body = _method_body(_driver_source(), "_subscribe_notify_channels")

    assert "self._rx_notify_handler = self._on_notify_channel_data" in body
    guard = body.index("if uuid in self._primed_notify_uuids:")
    assert guard < body.index("_start_notify_with_recovery(uuid)")
    assert "continue" in body[guard : body.index("_start_notify_with_recovery(uuid)")]


def test_every_method_that_primes_rx_also_releases_it():
    """기록과 CCCD 가 따로 놀면 그 채널은 조용히 죽는다.

    ``_subscribe_notify_channels`` 는 기록을 믿고 ``start_notify`` 를 건너뛴다.
    그러니 CCCD 를 내린 곳이 기록을 안 지우면, 메모리 세션은 아무도 켜지 않은
    디스크립터를 듣는다 — 클래식 언락 경로가 정확히 그랬고, 그 경로를 타는
    프로필이 카탈로그의 대부분이다.

    개수로 세지 않는다. 앞선 버전은 ``count(...) == 2`` 였고, 그래서 세 번째를
    더하는 **수정 자체를 실패시켰다**.
    """
    import re

    driver = _driver_source()
    lines = driver.splitlines()

    def method_of(idx):
        for j in range(idx, -1, -1):
            m = re.match(r"    (?:async )?def (\w+)\(", lines[j])
            if m:
                return m.group(1)
        return "?"

    primes = {
        method_of(i) for i, l in enumerate(lines) if "_primed_notify_uuids.add(" in l
    }
    releases = {
        method_of(i)
        for i, l in enumerate(lines)
        if "_release_primed_rx(" in l and "def _release_primed_rx" not in l
    }
    assert primes, "프라임하는 곳이 있어야 한다"
    assert primes <= releases, (
        f"프라임만 하고 해제하지 않는 메서드: {sorted(primes - releases)}"
    )

    # 해제 헬퍼는 stop 과 discard 를 같이 한다 — 둘이 갈라질 수 없게.
    helper = _method_body(driver, "_release_primed_rx")
    assert "stop_notify(uuid)" in helper
    assert "_primed_notify_uuids.discard(uuid)" in helper

    assert "_primed_notify_uuids.clear()" in _method_body(
        driver, "_unsubscribe_notify_channels"
    )


def test_rx_dispatcher_routes_only_when_a_handler_is_set():
    """실제로 호출해 본다 — 소스 문자열만 보면 배선 실수를 놓친다."""
    from custom_components.omron.omron_ble.omron_driver import OmronDeviceSession

    session = OmronDeviceSession(object(), _config())
    seen: list[bytes] = []

    session._rx_dispatch(None, bytearray(b"\x01\x02"))   # 핸들러 없음: 버린다
    assert seen == []

    session._rx_notify_handler = lambda _c, data: seen.append(bytes(data))
    session._rx_dispatch(None, bytearray(b"\x03\x04"))
    assert seen == [b"\x03\x04"]
