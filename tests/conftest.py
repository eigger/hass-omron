"""pytest 설정 — homeassistant/bleak 없이 단위 테스트 실행 가능하도록 mock.

sensor_state_data / bluetooth_sensor_state_data 도 함께 mock 한다: 실제 설치판을
쓰면 그 내부가 real habluetooth -> real bleak.backends.* 를 요구해 훨씬 무거운
의존성 체인으로 번진다. BluetoothData/BaseDeviceClass를 서브클래싱하는 코드는
MagicMock을 베이스로 둬도 임포트 시점에는 에러 없이 통과하므로(실제 인스턴스화나
상속된 동작을 쓰지 않는 한) 이 테스트 범위에서는 mock으로 충분하다.
"""
import sys
from unittest.mock import MagicMock


class MockBase:
    """제네릭 서브클래싱(예: BaseClass[T])을 지원하는 기본 Mock 클래스."""

    def __init__(self, *args, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls


# ── Mock Home Assistant Modules ────────────────────────────────────────────

sys.modules["homeassistant"] = MagicMock()
sys.modules["homeassistant.components"] = MagicMock()
sys.modules["homeassistant.components.bluetooth"] = MagicMock()
sys.modules["homeassistant.config_entries"] = MagicMock()
sys.modules["homeassistant.const"] = MagicMock()
sys.modules["homeassistant.core"] = MagicMock()
sys.modules["homeassistant.helpers"] = MagicMock()
sys.modules["homeassistant.helpers.device_registry"] = MagicMock()
sys.modules["homeassistant.util"] = MagicMock()
sys.modules["homeassistant.util.dt"] = MagicMock()

# OmronBluetoothProcessorCoordinator(PassiveBluetoothProcessorCoordinator[SensorUpdate])
# 처럼 실제로 서브클래싱 + 제네릭 첨자가 쓰이므로 MockBase 사용.
_ha_bt_processor = MagicMock()
_ha_bt_processor.PassiveBluetoothProcessorCoordinator = MockBase
sys.modules["homeassistant.components.bluetooth.passive_update_processor"] = _ha_bt_processor

# DataUpdateCoordinator[T] / CoordinatorEntity 도 서브클래싱 대비 MockBase.
_ha_update_coordinator = MagicMock()
_ha_update_coordinator.DataUpdateCoordinator = MockBase
_ha_update_coordinator.CoordinatorEntity = MockBase
sys.modules["homeassistant.helpers.update_coordinator"] = _ha_update_coordinator

# ── Mock bleak (BLE hardware I/O — 실제 설치 없이 타입 참조만 필요) ──────────

sys.modules["bleak"] = MagicMock()
sys.modules["bleak.backends.device"] = MagicMock()
sys.modules["bleak.exc"] = MagicMock()
sys.modules["bleak_retry_connector"] = MagicMock()

# ── Mock sensor_state_data / bluetooth_sensor_state_data / home_assistant_bluetooth ──

sys.modules["sensor_state_data"] = MagicMock()
sys.modules["bluetooth_sensor_state_data"] = MagicMock()
sys.modules["home_assistant_bluetooth"] = MagicMock()
