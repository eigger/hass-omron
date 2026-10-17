# 공통 규칙 적용 체크리스트

[사용법과 판정 기준](https://github.com/eigger/hass-repo-standards/blob/ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870/docs/checklist.md). 상태: `PASS` 근거 확인, `TODO` 미충족/미확인, `EXCEPTION` [STANDARDS.md](STANDARDS.md) 예외, `N/A` 해당 없음. 최초 검토이며 모든 항목은 TODO로 시작해 근거가 있는 것만 변경했습니다. 한 행에 묶은 규칙은 결과가 같은 것만 묶었습니다.

## 검토 대상

| 항목 | 값 |
|---|---|
| 리포 / 기본 브랜치 | eigger/hass-omron / master |
| domain / 구현 경로 | omron / custom_components/omron |
| 채택 PR / 커밋 | [#249](https://github.com/eigger/hass-omron/pull/249) (`7856739`), [#251](https://github.com/eigger/hass-omron/pull/251) (`08cc7fb`), 후속 소스 텍스트·AST 테스트 전환 및 전체 점검 반영 |
| 검토한 표준 SHA / 검토일 | ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870 (공통 workflow 호출 SHA와 동일) / 2026-10-18 |
| 조율 / 구현 / 독립 리뷰·검증 | 조율 `root` / 구현 `omron_implementation` / 독립 리뷰·검증 `omron_review` |
| 중앙 현황표 | 등록 완료 (`docs/adoption-status.md` `hass-omron` 행) |
| 지원 최소 HA | 2025.12.0 (`hacs.json`) — 변경 없음 |
| profile / 테스트 경로 | integration / ha, `tests/components/omron/` |
| 채택 판정 | 적용 대상 필수 항목 PASS / N/A (예외 0건) |

## 검증 환경

| 축 / 상태 | 통합 커밋 | Python / HA / 플러그인 | 대상 패키지 | 명령·결과 | 미검증 범위 |
|---|---|---|---|---|---|
| 로컬 current / PASS | `08cc7fb` + 후속 전환 | 3.14.5 / HA 2026.9.3 / 0.13.366 | blesession 0.9.0 | `ruff check .`, `ruff format --check .` 통과, `pytest tests/components/omron` 606 passed, `pip check` 이상 없음, `check_repository.py`·`sync_badges.py --check` 통과 | 실물 BLE 기기 |
| CI current / PASS | `08cc7fb` (PR #251) 및 최신 master | 3.14 / 2026.9.3 / 0.13.366 | blesession 최신 | `current / Python 3.14 / ha / current` pass (run 39040455377, 39039311253) | 실물 BLE 기기 |
| CI 최소 라이브러리 / PASS | `08cc7fb` (PR #251) 및 최신 master | 3.14 / 2026.9.3 / 0.13.366 | blesession==0.9.0 | `current / Python 3.14 / ha / minimum` pass (run 39040455377, 39039311253) | 실물 BLE 기기 |
| CI 최소 HA 2025.12.0 / PASS | `08cc7fb` (PR #251) 및 최신 master | 3.13 / 2025.12.0 / 0.13.298 | blesession>=0.9.0 | `minimum-ha / Python 3.13 / ha / current` pass (run 39040455377, 39039311253) | 최소 HA 로컬 미실행(CI에서 검증), 실물 BLE 기기 |

## 항목

| 규칙 ID | 상태 | 근거 / 남은 작업 |
|---|---|---|
| GOV-01 | PASS | `STANDARDS.md`에 전체 표준 SHA(`ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870`), profile(`integration / ha`), 기본 브랜치(`master`), 지원 최소 HA(`2025.12.0`) 기록 |
| GOV-02 | PASS | 공통 workflow 호출을 `ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870` 전체 SHA로 고정 (`.github/workflows/tests.yml`, `validate.yml`, `update_dependencies.yml`), `check_repository.py` 통과 |
| GOV-03 | PASS | 채택 PR [#249](https://github.com/eigger/hass-omron/pull/249) (`7856739`), [#251](https://github.com/eigger/hass-omron/pull/251) (`08cc7fb`) 및 후속 테스트 전환 커밋을 `master`에 반영 |
| GOV-04 | PASS | 중앙 `hass-repo-standards/docs/adoption-status.md`의 `hass-omron` 행에 기준 SHA·담당·진행 및 완료 상태 기록 |
| GOV-05 | PASS | 조율(`root`), 구현(`omron_implementation`), 독립 리뷰·검증(`omron_review`) 분리 위임 및 지적 사항 수정·재검증 |
| HA-01, HA-13 | PASS | 배포 경로 `custom_components/omron/`, `manifest.json` `"domain": "omron"`, `const.py` `DOMAIN = "omron"` 일치. `__init__.py`, `config_flow.py`, `coordinator.py`, 플랫폼 5종(`sensor.py`, `binary_sensor.py`, `button.py`, `number.py`, `text.py`), `omron_ble/` 라이브러리와 `tests/components/omron/` Core 대응 구조 |
| HA-02 | PASS | `custom_components/omron/manifest.json` 키 순서(`domain`, `name`, 이후 알파벳순) 확인, 자가 `quality_scale` 표기 없음 |
| HA-03 | PASS | `const.py`(상수), `config_flow.py`(UI/옵션 플로우), `coordinator.py`(`OmronBluetoothProcessorCoordinator`), 각 플랫폼 파일과 `omron_ble/` 프로토콜 라이브러리 분리 (`test_omron_ble_boundary.py`로 런타임 독립성 검증) |
| HA-04 | PASS | UI config flow(`custom_components/omron/config_flow.py` 69~395행: Bluetooth 자동 발견, 수동 선택, 모델 프로브/선택, 페어링 확인, 다사용자 별칭) 및 options flow(`OmronOptionsFlowHandler` 398~490행), `async_set_unique_id` 중복 방지 구현·검증 (`test_config_flow.py`, `test_config_flow_ScanInterval.py`, `test_model_selection_default.py`) |
| HA-05 | PASS | `OmronConfigEntry = ConfigEntry[OmronRuntimeData]`(`__init__.py` 80·774행), `async_unload_entry`(`__init__.py` 817~852행)에서 백그라운드 태스크·핸드오프 세션·플랫폼 정리 검증 (`test_entry_platforms.py`, `test_entry_lifecycle_behavior.py`, `test_pairing_session_handoff.py`) |
| HA-06 | PASS | 비동기 BLE 클라이언트(`bleak`/`blesession`), 명시적 폴 데드라인(`asyncio.timeout(POLL_TIMEOUT_SECONDS)`, `__init__.py` 596행), `CancelledError` 비삼킴(`parser.py` 1501행 `except Exception`)을 `test_poll_deadline.py`·`test_entry_lifecycle_behavior.py`로 검증 |
| HA-07 | PASS | 단일 `poll_coordinator`(`DataUpdateCoordinator[SensorUpdate]`)와 패시브 광고 `OmronBluetoothProcessorCoordinator` 사용, 폴 실패 시 `UpdateFailed` 발생으로 이전 정상 측정값 보존 (`__init__.py` 626·654행, `test_poll_failure_retention.py`, `test_entry_platforms.py`) |
| HA-08 | PASS | 안정적 엔티티 unique ID(`{address}_{key}`), 측정/진단 엔티티의 `SensorDeviceClass`·`SensorStateClass`·단위 설정, 기종별 조건부 엔티티(체중계 신장 `number`, 다사용자 슬롯 별칭) 노출 검증 (`test_entry_platforms.py`, `test_sensor_state_class.py`, `test_body_composition_entities.py`) |
| HA-09 | PASS | HA Bluetooth API(`async_ble_device_from_address`, `async_last_service_info`, `PassiveBluetoothProcessorCoordinator`) 및 ESPHome 프록시·로컬 BlueZ 경로 지원, `session_lock` 직렬화, disconnect/재연결/Service Changed/취소 검증 (`test_service_changed_order.py`, `test_session_cccd_persistence.py`, `test_connect_settle.py`, `test_stale_bond_guard.py`) |
| HA-10 | PASS | `blesession` 공개 API(`Notifications`, `guarded_write`, `start_notify_with_recovery`, `stop_notify_best_effort`, `probe_link`, `blesession.hass`, `blesession.testing.FakeClient`)만 사용하며 `omron_ble`는 `homeassistant`/`blesession.hass` 미참조 (`test_omron_ble_boundary.py`) |
| HA-11 | PASS | `strings.json` 및 15개 `translations/*.json`이 실제 config/options 단계 및 엔티티 번역 키와 일치 (`test_entity_name_translation.py`, `test_config_flow.py`, `validate / hassfest`) |
| HA-12 | PASS | 테스트 fixture에 민감 정보 없음, 기기 수면·범위 밖 등 일상적 BLE 실패는 `UpdateFailed`와 debug 로그로 처리해 반복 경고 누적 방지 (`__init__.py` 639~654행, `test_poll_failure_retention.py`) |
| TEST-08 | PASS | 전체 테스트 모듈을 `tests/components/omron/`에 배치 |
| TEST-02, TEST-03, TEST-09 | PASS | 기존 Python 소스 텍스트·AST·`inspect.getsource` 검사 23개 모듈을 전부 실제 HA 통합(`hass` fixture, `MockConfigEntry`) 및 런타임 동작 테스트로 전환 완료 (남은 `.py` 소스 텍스트/AST 검사 0개). `test_entry_platforms.py`, `test_entry_lifecycle_behavior.py`, `test_config_flow.py`, `test_pairing_session_handoff.py`, `test_transport_credential.py`, `test_display_model.py`, `test_poll_failure_retention.py` 등 15개 모듈이 실제 HA 런타임·플랫폼·레지스트리·서비스 호출을 검증하고 나머지 모듈은 순수 프로토콜/파서 동작을 검증 |
| TEST-01, TEST-06, TEST-07 | PASS | 606개 테스트가 config/options flow(성공·실패·중복·별칭), entry setup/unload/reload/remove, 5개 플랫폼 상태·action, BLE 광고·핸드오프·타임아웃·취소·연결 끊김·재시도·이력 동기화를 외부 BLE 경계(`FakeClient`/`FakeSession`)만 대체해 검증 |
| TEST-04, HACS-01(최소 검증) | PASS | `minimum-ha` job이 `hacs.json` 최소 버전 HA 2025.12.0(Python 3.13, plugin 0.13.298, `expected-ha-version: 2025.12.0`)에서 전체 테스트 통과 |
| TEST-05, DEP-02, DEP-05 | PASS | `current` job의 `minimum` 모드에서 `blesession==0.9.0` 설치 확인 및 전체 테스트 통과, `manifest.json`의 `>=0.9.0` 연산자 유지 |
| DEP-01, DEP-06 | PASS | 런타임 의존성은 `custom_components/omron/manifest.json`(`requirements: ["blesession>=0.9.0"]`, `dependencies: ["bluetooth_adapters"]`), 테스트·린트 의존성은 `requirements_test.txt`·`requirements_test_min.txt`·`requirements_lint.txt`로 분리 |
| DEP-07, CI-06 | PASS | 공통 `install.py`가 단일 resolver 호출 및 `pip check`, HA 구성요소(`bluetooth`, `bluetooth_adapters`, `usb`) 요구사항을 함께 설치하고 `ruff==0.17.1` 고정 |
| DEP-03, DEP-04, CI-09, CI-10 | PASS | `.github/workflows/update_dependencies.yml`이 공통 `dependency-update.yml`을 호출하며 `validation-command`로 `ruff`와 `pytest tests/components/omron/`을 실행. 로컬 `actions/update-manifest/update.py` dry-run 검증(`Requirements are current; no change`) 및 `.github/dependabot.yml`의 최소 환경 `exclude-paths` 설정 확인. 열린 Dependabot PR #250(`pytest-homeassistant-custom-component` 0.13.366→0.13.372)에서 `minimum-ha` 파일 미변경 및 `expected-ha-version: 2026.9.3` 불일치 차단 확인(자동 병합하지 않음) |
| DEP-08 | N/A | 이번 채택에서 신규 런타임 의존성 추가나 기존 요구사항 범위 변경 없음 |
| HACS-01(구조), HACS-02 | PASS | 루트 `README.md`, `LICENSE`, `hacs.json` 및 `custom_components/omron` 구조 확인 |
| HACS-03, HACS-05 | PASS | HACS Custom 배지 사용, `validate / hacs` 및 `validate / hassfest` 통과, `custom_components/omron/brand/icon.png`(256×256)·`icon@2x.png`(512×512) 존재 |
| HACS-04 | PASS | 최신 GitHub 정식 Release 태그 `4.1.1`(`isPrerelease: false`)과 `custom_components/omron/manifest.json` `"version": "4.1.1"` 일치 |
| HACS-06 | PASS | GitHub 리포 description(`Omron BLE Home Assistant Integration`), topics, Issues/Discussions 활성화 및 `hacs.json` 유효 필드(`name`, `homeassistant`) 확인 |
| CI-01, CI-02 | PASS | `Tests`, `Validate`, `Update dependencies`가 공통 reusable workflow를 호출하며 `master` push, PR, `workflow_dispatch`, 주간 `schedule` 지원 |
| CI-03, CI-05 | PASS | 검증 workflow `permissions: contents: read`, `concurrency` 설정, 공통 workflow 전체 SHA 고정 |
| CI-04, CI-07, CI-12 | PASS | 필수 `ha` 테스트 job(`current`, `minimum-ha`)과 `lint` 병렬 실행, 오류 무시 래퍼 없음, `master` 브랜치 보호 미사용 확인 |
| CI-08 | PASS | 모든 workflow 파일에 비신뢰 PR 입력의 직접 shell 보간 없음, `pull_request_target` 미사용, 테스트에 secrets 불요, `secrets: inherit` 미사용 |
| CI-11 | N/A | 자동 릴리스 생성 workflow를 두지 않으며 수동 GitHub Release(`4.1.1`)로 배포 |
| DOC-01, DOC-06 | PASS | `README.md`를 표준 섹션 순서(소개 → 지원 모델 → 요구사항 → 설치·설정 → 빠른 시작 → 중요 제한사항 → 문서 → 지원 → 라이선스)로 정리하고 오류별 대응을 `docs/troubleshooting.md`로 이동·연결. `CONTRIBUTING.md`에 Core 대응 구조·실제 HA 테스트·의존성 절차·검증 환경 표 수록 |
| DOC-02 | PASS | `README.md`(영어)와 `README.ko.md`(한국어) 상호 전환 링크, 동일 배지·문서 목록·제한사항 유지 |
| DOC-03, DOC-04, DOC-05 | PASS | `sync_badges.py --check` 통과 (HACS → Release → HA minimum → Tests → Validate → License → 동적 Usage 순서) |
| DOC-07 | PASS | `README.md`·`README.ko.md`에서 공통 Bluetooth 문서(고정 SHA)와 `docs/troubleshooting.md` 연결, 기기 고유 깨우기·`-P-` 페어링·단일 앱 제약·진단 센서(`Last Failure`, `Duration`) 및 민감 정보 제거 후 제보 절차 안내 |
| REL-01, REL-02, REL-03, REL-05 | PASS | SemVer(`4.1.1`) 준수, 규칙 채택만을 위한 태그/릴리스 미생성, 최소 HA `2025.12.0` 유지, 채택·테스트 개편을 기능 릴리스와 분리 |
| REL-04 | N/A | 현행 유지보수 리포로 이전·중단 해당 없음 |

## 남은 작업

1. 현재 표준 SHA(`ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870`) 기준 필수 미충족·예외 없음. 실물 BLE 혈압계/체중계 하드웨어 검증은 별도 기기 보유 시 수행.
