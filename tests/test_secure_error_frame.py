"""_secure_error_frame_code 단위 테스트.

기기가 ECDH secure-session 요청을 거부할 때 보내는 에러 프레임(0xff + code)을
정상 응답 헤더(0xf0.. / 0xc0)와 구분해 코드를 추출하는지 검증한다. ff26 값은
실기기(HEM-7188T1-LEO) 디버그 로그의 "Received Pairing Response (len=2): ff26"
에서 그대로 가져왔다.
"""
from custom_components.omron.omron_ble.omron_driver import (
    _secure_error_frame_code,
)


class TestSecureErrorFrameCode:
    def test_ff26_returns_code(self):
        # 실기기 HEM-7188T1-LEO 가 pair request 거부 시 보낸 프레임.
        assert _secure_error_frame_code(bytes.fromhex("ff26")) == 0x26

    def test_ff_with_longer_payload_returns_code(self):
        assert _secure_error_frame_code(bytes.fromhex("ff2600aa")) == 0x26

    def test_valid_pair_response_header_is_not_error(self):
        # 정상 pair response 는 0xf0 0x81 로 시작한다.
        assert _secure_error_frame_code(bytes.fromhex("f081deadbeef")) is None

    def test_valid_enc_and_challenge_headers_are_not_error(self):
        assert _secure_error_frame_code(bytes.fromhex("f085")) is None
        assert _secure_error_frame_code(bytes.fromhex("f086")) is None

    def test_encrypted_data_header_is_not_error(self):
        assert _secure_error_frame_code(bytes.fromhex("c000000000")) is None

    def test_none_and_short_return_none(self):
        assert _secure_error_frame_code(None) is None
        assert _secure_error_frame_code(b"") is None
        assert _secure_error_frame_code(b"\xff") is None
