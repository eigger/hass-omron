# Omron BLE Home Assistant 통합 (hass-omron)

<!-- hass-standards:badges:start -->
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5?style=flat-square)](#설치)
[![Release](https://img.shields.io/github/v/release/eigger/hass-omron?style=flat-square)](https://github.com/eigger/hass-omron/releases)
[![Home Assistant minimum](https://img.shields.io/badge/HA-%E2%89%A5%202025.12.0-41BDF5?style=flat-square)](hacs.json)
[![Tests](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/tests.yml?branch=master&style=flat-square&label=Tests)](https://github.com/eigger/hass-omron/actions/workflows/tests.yml)
[![Validate](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/validate.yml?branch=master&style=flat-square&label=Validate)](https://github.com/eigger/hass-omron/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/eigger/hass-omron?style=flat-square)](LICENSE)
[![Analytics reporting installs](https://img.shields.io/badge/dynamic/json?style=flat-square&color=41BDF5&label=Usage&query=%24.omron.total&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json)](https://analytics.home-assistant.io/)
<!-- hass-standards:badges:end -->

[English](README.md) | **한국어**

Omron Bluetooth Low Energy(BLE) 혈압계와 체중계에 직접 연결해 측정값을 가져오는 Home Assistant 커스텀 통합(HACS)입니다. Home Assistant Core에 포함된 공식 통합이 아닙니다.

## 지원 기기

혈압계 HEM-6161T, HEM-6232T, HEM-7142T2, HEM-7146T2, HEM-7151T, HEM-7155T, HEM-7156T, HEM-716BT2, HEM-7188T1, HEM-7320T, HEM-7322T, HEM-7343T, HEM-7374T1, HEM-7380T1, HEM-7382T1, HEM-7530T, HEM-7600T와 체중계 HN-300T2, HBF-222T를 지원합니다. 전체 표와 검증 여부는 [영문 README](README.md#supported-models)를 따릅니다. 표에 없는 모델은 비슷한 모델을 선택하면 동작할 수 있으나 정식 검증되지 않았습니다.

## 요구사항

- Home Assistant 2025.12.0 이상(`hacs.json` 기준)
- Bluetooth 어댑터 또는 ESPHome Bluetooth 프록시(프록시 사용을 강력히 권장)

## 설치

1. **HACS**: `eigger/hass-omron`을 HACS 커스텀 리포지토리(Integration)로 추가하고 설치합니다. 수동 설치는 `custom_components/omron`을 Home Assistant의 `custom_components`에 복사합니다.
2. Home Assistant를 재시작합니다.
3. **설정 → 기기 및 서비스**에서 Omron을 추가합니다.

## 설정

혈압계의 Bluetooth 버튼을 3~5초 눌러 `-P-` 표시가 깜박이게 한 뒤, 통합을 추가하고 모델을 선택한 다음 `-P-`가 깜박이는 동안 **제출**을 누릅니다. 성공하면 기기에 `[OK]`가 표시됩니다. 자세한 단계는 [영문 README](README.md#pairing--configuration)를 참고하세요.

## 빠른 시작

ESPHome 프록시를 사용할 때의 권장 설정(기본 스캔 간격 유지):

```yaml
esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

## 중요 제한사항

- **Omron 혈압계는 한 번에 한 기기와만 페어링됩니다.** 공식 Omron Connect 앱과 페어링했다면 휴대폰 Bluetooth 설정에서 먼저 연결을 해제해야 합니다.
- `bluetooth_proxy`는 반드시 `active: true`여야 합니다. 프록시 설정·추천 기기·연결 문제 확인 순서는 [공통 Bluetooth 안내](https://github.com/eigger/hass-repo-standards/blob/ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870/docs/bluetooth.md), 모델별 조건과 진단은 [문제 해결](docs/troubleshooting.md#bluetooth-proxy-and-model-specific-conditions)(영어)을 참고하세요.
- 표에 없는 모델은 정식 검증되지 않았습니다. 문제가 있으면 디버그 로그와 함께 이슈로 알려 주세요.

## 문서

상세 문서는 모두 영어로 제공됩니다.

- [엔티티·분류·폴링 동작](docs/entities.md) (영어)
- [문제 해결](docs/troubleshooting.md) (영어)
- [Bluetooth 로그 수집](docs/capturing-bluetooth-logs.md) (영어)
- [HEM-7374T1 노트](docs/hem-7374t1.md) (영어)
- [로드맵](docs/roadmap.md) (영어)
- [개발과 테스트](CONTRIBUTING.md) (영어)

## 지원

- 버그 보고: [Issues](https://github.com/eigger/hass-omron/issues)
- 질문·기능 제안: [Discussions](https://github.com/eigger/hass-omron/discussions)

## 라이선스

[LICENSE](LICENSE)를 참고하세요.
