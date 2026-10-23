<!-- hass-standards:header:start -->
# Omron BLE Home Assistant Integration (hass-omron)

Omron Bluetooth Low Energy(BLE) 혈압계와 체중계에 직접 연결해 측정값을 가져오는 Home Assistant 커스텀 통합(HACS)입니다. Home Assistant Core에 포함된 공식 통합이 아닙니다.

[English](README.md) | **한국어**

<!-- hass-standards:badges:start -->
[![Release](https://img.shields.io/github/v/release/eigger/hass-omron?style=flat)](https://github.com/eigger/hass-omron/releases)
[![HACS Default](https://img.shields.io/badge/HACS-Default-41BDF5?style=flat)](#설치)
[![Home Assistant minimum](https://img.shields.io/badge/HA-%E2%89%A5%202025.12.0-41BDF5?style=flat)](hacs.json)
[![Tests](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/tests.yml?branch=master&style=flat&label=Tests)](https://github.com/eigger/hass-omron/actions/workflows/tests.yml)
[![Validate](https://img.shields.io/github/actions/workflow/status/eigger/hass-omron/validate.yml?branch=master&style=flat&label=Validate)](https://github.com/eigger/hass-omron/actions/workflows/validate.yml)
[![License](https://img.shields.io/github/license/eigger/hass-omron?style=flat)](LICENSE)
[![Downloads](https://img.shields.io/github/downloads/eigger/hass-omron/total?style=flat&color=41BDF5&label=Downloads)](https://github.com/eigger/hass-omron/releases)
<!-- hass-standards:badges:end -->
<!-- hass-standards:header:end -->

Downloads 배지는 모든 GitHub 릴리스의 첨부 asset 다운로드 합계이며 활성 설치 수가 아닙니다.

| 기기 | 모델 |
| :---: | :---: |
| <img src="https://raw.githubusercontent.com/eigger/hass-omron/master/docs/images/bpm.jpg" width="280" alt="Omron HEM-7142T2"> | **HEM-7142T2** |

## 지원 기기

| 모델 | 글로벌 모델 | 종류 | 검증 여부 |
| :--- | :--- | :--- | :---: |
| **HEM-6161T** | RS2 Intelli IT | 손목형 | ✅ |
| **HEM-6232T** | BP6350/RS7 Intelli IT | 손목형 | ✅ |
| **HEM-7142T2** | BP7150/M2 Intelli IT / U705T (중국) | 상완형 | ✅ |
| **HEM-7146T2** | X2 Smart+ | 상완형 | ✅ |
| **HEM-7151T** | BP5250/M3 Comfort | 상완형 | ✅ |
| **HEM-7155T** | BP7350/M4 Intelli IT | 상완형 | ✅ |
| **HEM-7156T** | BP7250 | 상완형 | ✅ |
| **HEM-716BT2** | BP5255/M4 Smart | 상완형 | ✅ |
| **HEM-7188T1** | M2+ / X2+ Connect | 상완형 | ✅ |
| **HEM-7320T** | BP7450/M7 Intelli IT | 상완형 | ✅ |
| **HEM-7322T** | BP786/M6 Intelli IT | 상완형 | ✅ |
| **HEM-7343T** | BP5450/Platinum Series | 상완형 | ✅ |
| **HEM-7374T1** | U738T (중국) | 상완형 | ✅ |
| **HEM-7380T1** | M7 Intelli IT AFib / X7 Smart AFib | 상완형 | ✅ |
| **HEM-7382T1** | BP5465 | 상완형 | ✅ |
| **HEM-7530T** | BP7900/Complete | 상완형 | ✅ |
| **HEM-7600T** |	BP7000/EVOLV	| 상완형	| ✅ |
| **HN-300T2** | HN-300T2 | 체중계 | ✅ |
| **HBF-222T** | VIVA / BCM-500 | 체중계 | ✅ |

- **HN-300T2**: 체중과 설정한 키를 바탕으로 계산한 BMI를 제공합니다.
- **HBF-222T**: 체중, 체성분, 체중계가 보고한 BMI와 저장된 측정 이력을 제공합니다.

> [!NOTE]
> 다른 Omron BLE 기기도 설정할 때 비슷한 모델을 선택하면 동작할 수 있지만 정식으로 테스트하지 않았습니다. 기기가 동작하지 않으면 Home Assistant 디버그 로그와 가능하다면 [휴대폰 Bluetooth 스택 로그](docs/capturing-bluetooth-logs.md)(영어)를 공유해 주세요.

## 요구사항

Home Assistant 2025.12.0 이상(`hacs.json` 참고)과 Bluetooth 어댑터 또는 ESPHome Bluetooth 프록시가 필요합니다. 연결 안정성과 통신 범위를 위해 Bluetooth 프록시 사용을 강력히 권장합니다.

## 설치

<!-- hass-standards:installation:start -->
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eigger&repository=hass-omron)
<!-- hass-standards:installation:end -->

1. **HACS**: HACS가 설치된 상태에서 HACS를 열고 **Omron**을 검색한 뒤 통합을 열어 다운로드합니다. HACS 기본 저장소 목록에 포함되어 있으므로 커스텀 저장소를 추가할 필요가 없습니다.
   **수동 설치**: 또는 `custom_components/omron` 디렉터리를 Home Assistant의 `custom_components` 폴더에 복사합니다.
2. Home Assistant를 재시작합니다.

## 페어링 및 설정

기기 설정과 페어링은 모두 Home Assistant UI에서 진행합니다.

<p align="center">
  <img src="https://raw.githubusercontent.com/eigger/hass-omron/master/docs/images/pairing.jpg" width="400" alt="페어링 모드">
</p>

1. **페어링 모드 진입**:
   - Omron 혈압계의 **Bluetooth 버튼을 3~5초 동안 길게 누릅니다**. 모델에 따라 해당 연결 버튼을 사용합니다.
   - 화면에 **`-P-`** 기호가 깜박이면 페어링할 준비가 된 것입니다.
2. **통합 추가**:
   - Home Assistant에서 **설정** > **기기 및 서비스**로 이동합니다.
   - Bluetooth를 통해 "Omron" 기기가 자동으로 검색되면 **구성**을 누릅니다.
   - 자동으로 검색되지 않으면 **통합 구성요소 추가**를 누르고 "Omron"을 검색합니다.
3. **모델 선택**:
   - 드롭다운 목록에서 정확한 Omron 모델을 선택합니다.
4. **페어링 완료**:
   - 혈압계에서 `-P-`가 계속 깜박이는지 확인합니다.
   - Home Assistant에서 **제출**을 누릅니다.
   - Home Assistant가 페어링 키를 기기에 기록하거나 운영체제 수준의 본딩을 수행해 안전하게 페어링합니다.
   - 성공하면 혈압계 화면에 `[OK]` 기호가 표시됩니다.

## 빠른 시작

ESPHome Bluetooth 프록시 권장 설정입니다. 기본 스캔 간격을 유지하세요.

```yaml
esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true
```

## 중요 제한사항

- **한 번에 하나의 호스트와만 페어링**: Omron 혈압계는 한 번에 한 기기와만 페어링할 수 있습니다. 공식 Omron Connect 스마트폰 앱과 이미 페어링했다면 Home Assistant에 연결하기 전에 휴대폰 Bluetooth 설정에서 **페어링 해제/기기 삭제**를 해야 합니다. 하드웨어 제약으로 두 기기를 동시에 사용할 수 없습니다.
- **Bluetooth 프록시 요구사항**: `bluetooth_proxy`는 항상 `active: true`여야 하며, `scan_parameters`는 기본 간격을 유지해야 합니다. 하드웨어 추천은 [ESPHome Bluetooth 프록시용 ESP32 보드 추천](https://community.home-assistant.io/t/great-esp32-board-for-an-esphome-bluetooth-proxy/916767/31)(영어)을 참고하세요. 프록시 설정, 추천 기기와 연결 문제 확인 순서는 [Bluetooth 프록시 설정](https://github.com/eigger/blesession/blob/main/docs/bluetooth.ko.md)에 있습니다. 모델별 깨우기·페어링 조건, 오류별 해결 방법과 진단(`Last Failure`, `Duration`)은 [문제 해결](docs/troubleshooting.ko.md)을 참고하세요.
- **미검증 모델**: 지원 기기 표에서 검증 여부가 표시되지 않은 모델은 검증되지 않았습니다.

## 문서

문제 해결은 한국어로 제공하며 나머지 상세 문서는 영어로 제공됩니다.

- [엔티티·분류·폴링 동작](docs/entities.md) (영어)
- [문제 해결](docs/troubleshooting.ko.md)
- [Bluetooth 로그 수집](docs/capturing-bluetooth-logs.md) (영어)
- [HEM-7374T1 노트](docs/hem-7374t1.md) (영어)
- [체성분 체중계 노트](docs/body-composition-scales-notes.md) (영어)
- [HN-300T2 메모리 맵](docs/hn-300t2-memory-map.md) (영어)
- [로드맵](docs/roadmap.md) (영어)
- [개발과 테스트](CONTRIBUTING.md) (영어)
- 참고: [omblepy (userx14)](https://github.com/userx14/omblepy)

<!-- hass-standards:support:start -->
## 지원

- [Issues](https://github.com/eigger/hass-omron/issues): 버그를 신고하거나 기능을 요청하세요.
- [Discussions](https://github.com/eigger/hass-omron/discussions): 질문하고 사용 경험을 공유하세요.
<!-- hass-standards:support:end -->

<!-- hass-standards:related-projects:start -->
## 관련 프로젝트

- [blesession](https://github.com/eigger/blesession): 이 통합에서 사용하는 공용 BLE 세션과 Bluetooth 프록시 안내입니다.
- [omblepy](https://github.com/userx14/omblepy): Omron BLE 통신 참고 구현입니다.
<!-- hass-standards:related-projects:end -->

<!-- hass-standards:license:start -->
## 라이선스

[MIT License](LICENSE) 라이선스로 배포됩니다.
<!-- hass-standards:license:end -->
