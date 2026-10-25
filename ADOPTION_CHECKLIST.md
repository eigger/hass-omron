# 공통 규칙 적용 체크리스트

## 2026-10-21 최신 표준·HACS Default 재적용

- 검토 기준: `0ec31111a81b0ee5702f1d8a17f03616a10a5db6` (중앙 main과 local HEAD 동일, `git ls-remote` 확인).
- 검증 대상: `a068c1bdb72c428cded73365f936e26008e7fa4d` 기반 로컬 작업 트리. 커밋 전 작업 트리에 대한 아래 검증이며 과거 커밋/원격 CI 결과와 구분합니다. 사용자 요청으로 동일 변경을 PR에 제출합니다.
- 담당: 조율 `root`, 문서 구현 `docs_implementation`, README·테스트 구조 구현 `readme_implementation`, 독립 리뷰·최소 HA 검증 `independent_review`.
- HACS Default 근거: [공식 integration 목록](https://github.com/hacs/default/blob/master/integration)에 `eigger/hass-omron` 등재를 2026-10-21 구현·독립 조사에서 확인.
- 지원 최소 HA `2025.12.0`, 통합 `4.1.2`, 런타임 의존성 하한 유지.

| 변경·규칙 | 상태 | 근거 |
|---|---|---|
| GOV-01, GOV-02, CI-01–03 | PASS (로컬) | 3개 reusable caller를 검토 full SHA로 고정, Tests·updater `standards-ref` 동기화, Validate에 중앙 template compliance job 추가. `STANDARDS.md` 기준 필드 갱신 |
| HA-02 | PASS | `categories: ["health_wellbeing"]` 및 `categories_review` 판단 기록. 실제 혈압계·체중/체성분계 제품 용도에 따른 선택으로 독립 확인 |
| HACS-03, DOC-03–05 | PASS | Default·공식 HACS Downloads 배지 적용, Custom Usage 교체, 중앙 `sync_badges.py --check` 통과. 다운로드 수는 활성 설치 수를 의미하지 않음 |
| DOC-01, DOC-02, DOC-06 | PASS | 중앙 renderer로 양언어 header/support/related-projects/license 및 My HACS 설치 배지 생성. 영어 모델표·설정·YAML 보존, 한국어 누락 정보 전체 보완. 실제 Default 검색 설치 절차 안내 |
| DOC-07 | PASS | 양언어 blesession 사용자 원본 연결, `docs/troubleshooting.md`·`docs/troubleshooting.ko.md` 고정 구조·기기 조건·증상표·진단·로그·기존 Issues 연결. 독립 리뷰가 실제 `sensor.py`·`session_report.py`·`const.py`와 대조 |
| TEST-03, TEST-08, HA-13 | PASS | 중앙 exact root conftest·3개 빈 init·domain fixture 관리 블록·빈 `custom_components/__init__.py` 적용. 기존 테스트를 init/각 플랫폼/coordinator 파일로 재배치. 독립 AST 비교에서 test 함수명 multiset 전후 573개 동일. 최소 HA event-loop 호환 fixture 보존 |
| HA-11 | PASS | `strings.json` Core 참조를 기존 영어 번역 문구로 해소해 en.json과 일치. 한국어 기기 선택·중복·재인증 누락 문구 보완; 중앙 키/placeholder 검사 통과 |
| GOV-05 | PASS | 구현 담당과 별도 독립 리뷰·최소 HA 전체 실행, 필수 지적 없음 |
| GOV-04 | 로컬 반영 / 원격 TODO | 중앙 `docs/adoption-status.md`의 hass-omron 행만 갱신. 기존 다른 리포 수정 보존. 원격 중앙 반영·대상 기본 브랜치 반영은 미실행 (대상 변경은 PR 제출) |

### 실제 검증

| 환경 / 검사 | 버전·명령 | 결과·한계 |
|---|---|---|
| current HA + 정확한 라이브러리 하한 | Python 3.14.5 / HA 2026.10.2 / plugin 0.13.373 / blesession 0.9.0 / bluetooth-sensor-state-data 1.7.0 / sensor-state-data 2.18.0; `python -m pytest tests/components/omron/ -q --timeout 120` | **630 passed** (12.54초). 별도 `/private/tmp/omron-current-20261021` 환경, 요구사항·HA bluetooth/bluetooth_adapters/usb requirements를 함께 resolve, `uv pip check` 통과. 로그 `/private/tmp/omron-current-final-20261021.log` |
| 최소 HA / 독립 최종 실행 | Python 3.13.14 / HA 2025.12.0 / plugin 0.13.298 / blesession 0.9.0 / bluetooth-sensor-state-data 1.9.0 / sensor-state-data 2.20.0; 동일 pytest 명령, 기존 `constraints/homeassistant-min.txt` 적용 | **629 passed, 1 skipped, 1 warning** (11.08초), `uv pip check` 통과. `/private/tmp/omron-independent-minimum-20261021`, 로그 `/private/tmp/omron-independent-minimum-final-pytest.log` |
| 중앙 정적 검사 | `python <central>/scripts/check_integration.py . --check`, `sync_badges.py . --check`, `check_repository.py` | 모두 통과. 독립 리뷰에서도 integration·badge 검사 재실행 통과. `manage_integration.py inspect` 결과 conflicts/pending 없음 |
| Ruff / workflow | `python -m ruff check .`, `python -m ruff format --check .` (ruff 0.17.1), `actionlint -oneline .github/workflows/*.yml` (1.7.12), `git diff --check` | 모두 통과 |
| 로컬 Hassfest | 기존 `minibig-hassfest:ec18d8` 이미지, `docker run --rm --network none -v <target>:/github/workspace:ro minibig-hassfest:ec18d8` | Integrations 1 / Invalid integrations 0. 이미지 내 Core snapshot에 대한 로컬 검증이며 새 원격 CI 성공을 뜻하지 않음. 로그 `/private/tmp/omron-hassfest-final-20261021.log` |

### PR #254 최초 CI 및 후속 수정

- 대상 커밋 `4d748416c0109f5f6de507b7ff9b50f14284dcbc`: [Tests 39598680489](https://github.com/eigger/hass-omron/actions/runs/39598680489) 모든 current·minimum-library·minimum-HA·lint job PASS.
- [Validate 39598680526](https://github.com/eigger/hass-omron/actions/runs/39598680526): HACS·Central template compliance PASS, Hassfest FAIL (`Invalid manifest: not a valid option at 'categories'`). 로컬 검증과 달리 published 기본 이미지에 새 schema가 없었음.
- 수정: `validate.yml`의 공용 validate job에 검토한 Core `ec18d8b25d1ee39aa8b85a72a170e42bee33228c`를 `hassfest-core-ref`로 전달. 중앙 기존 경로로 upstream 공식 Dockerfile/전체 validator를 빌드·실행하며 manifest 전처리나 검사 제외 없음. 수정 이후 원격 결과는 새 실행에서 확인.

### 남은 외부 검증·반영

- 로컬 검증 시점에는 현재 작업 트리의 GitHub Actions HACS/Hassfest/Tests 및 중앙 원격 반영을 실행하지 않았습니다. PR 제출 후 발생하는 CI 결과는 별도로 확인합니다. 아래 과거 PASS 기록을 새 SHA의 CI 증거로 사용하지 않습니다.
- 실물 BLE 기기·로컬 어댑터/ESPHome 프록시 검증은 이번 작업에서 수행하지 않았습니다.
- 참고: 기존 `/private/tmp/cur` 환경은 `sensor_state_data` 미설치로 수집 실패하여 사용하지 않고, 별도 완전한 current 환경에서 다시 설치·전체 검증했습니다.

---

# 2026-10-18 적용 기록 (과거 기준)

[사용법과 판정 기준](https://github.com/eigger/hass-repo-standards/blob/ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870/docs/checklist.md). 상태: `PASS` 근거 확인, `TODO` 미충족/미확인, `EXCEPTION` [STANDARDS.md](STANDARDS.md) 예외, `N/A` 해당 없음. 최초 검토이며 모든 항목은 TODO로 시작해 근거가 있는 것만 변경했습니다. 한 행에 묶은 규칙은 결과가 같은 것만 묶었습니다.

## 검토 대상

| 항목 | 값 |
|---|---|
| 리포 / 기본 브랜치 | eigger/hass-omron / master |
| domain / 구현 경로 | omron / custom_components/omron |
| 채택 PR / 커밋 | [#249](https://github.com/eigger/hass-omron/pull/249) (`7856739`), [#251](https://github.com/eigger/hass-omron/pull/251) (`08cc7fb`), `f9cfc8a`, `2e6d405`, `d34ab1b`, `2ae7b74`, `aaf60f1` |
| 검토한 표준 SHA / 검토일 | ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870 (공통 workflow 호출 SHA와 동일) / 2026-10-18 |
| 조율 / 구현 / 독립 리뷰·검증 | 조율 `root` / 구현 `omron_implementation` / 독립 리뷰·검증 `omron_review` |
| 중앙 현황표 | 등록 완료 (`docs/adoption-status.md` `hass-omron` 행) |
| 지원 최소 HA | 2025.12.0 (`hacs.json`) — 변경 없음 |
| profile / 테스트 경로 | integration / ha, `tests/components/omron/` |
| 채택 판정 | 적용 대상 필수 항목 PASS / N/A (예외 0건) |

## 검증 환경

| 축 / 상태 | 통합 커밋 | Python / HA / 플러그인 | 대상 패키지 | 명령·결과 | 미검증 범위 |
|---|---|---|---|---|---|
| 로컬 current / PASS | `aaf60f1` | 3.14.5 / HA 2026.9.3 / 0.13.366 | blesession 0.9.0 | `ruff check .`, `ruff format --check .` 통과, `pytest tests/components/omron` 606 passed, `pip check` 이상 없음, `check_repository.py`·`sync_badges.py --check` 통과 | 실물 BLE 기기 |
| CI current / PASS | `aaf60f1` | 3.14 / 2026.10.2 / 0.13.373 | blesession 최신 | `current / Python 3.14 / ha / current` 및 `Validate` (`hacs`, `hassfest`) pass (Tests run 39048112945, Validate run 39048112957) | 실물 BLE 기기 |
| CI 최소 라이브러리 / PASS | `aaf60f1` | 3.14 / 2026.10.2 / 0.13.373 | blesession==0.9.0 | `current / Python 3.14 / ha / minimum` pass (Tests run 39048112945) | 실물 BLE 기기 |
| CI 최소 HA 2025.12.0 / PASS | `aaf60f1` | 3.13 / 2025.12.0 / 0.13.298 | blesession>=0.9.0 | `minimum-ha / Python 3.13 / ha / current` pass (Tests run 39048112945) | 최소 HA 로컬 미실행(CI에서 검증), 실물 BLE 기기 |

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
| TEST-08 | PASS | Core 대응 구조(`tests/__init__.py`, `tests/conftest.py`, `tests/components/__init__.py`, `tests/components/omron/__init__.py`, `tests/components/omron/conftest.py`, `tests/components/omron/fixtures/eeprom_time_offsets.json` 및 테스트 모듈)로 배치 |
| TEST-02, TEST-03, TEST-09 | PASS | 기존 Python 소스 텍스트·AST·`inspect.getsource` 검사 23개 모듈을 전부 실제 HA 통합(`hass` fixture, `MockConfigEntry`) 및 런타임 동작 테스트로 전환 완료 (남은 `.py` 소스 텍스트/AST 검사 0개). `test_entry_platforms.py`, `test_entry_lifecycle_behavior.py`, `test_config_flow.py`, `test_pairing_session_handoff.py`, `test_transport_credential.py`, `test_display_model.py`, `test_poll_failure_retention.py` 등 15개 모듈이 실제 HA 런타임·플랫폼·레지스트리·서비스 호출을 검증하고 나머지 모듈은 순수 프로토콜/파서 동작을 검증 |
| TEST-01, TEST-06, TEST-07 | PASS | 629개 테스트가 config/options flow(성공·실패·중복·별칭), entry setup/unload/reload/remove, 5개 플랫폼 상태·action, BLE 광고·핸드오프·타임아웃·취소·연결 끊김·재시도·이력 동기화를 외부 BLE 경계(`FakeClient`/`FakeSession`)만 대체해 검증 |
| TEST-04, HACS-01(최소 검증) | PASS | `minimum-ha` job이 `hacs.json` 최소 버전 HA 2025.12.0(Python 3.13, plugin 0.13.298, `expected-ha-version: 2025.12.0`)에서 전체 테스트 통과 |
| TEST-05, DEP-02, DEP-05 | PASS | `current` job의 `minimum` 모드에서 `blesession==0.9.0`, `bluetooth-sensor-state-data==1.7.0`, `sensor-state-data==2.18.0` 설치 확인 및 전체 테스트 통과, `manifest.json`의 `>=0.9.0` 연산자 유지 |
| DEP-01, DEP-06 | PASS | 런타임 의존성은 `custom_components/omron/manifest.json`(`requirements: ["blesession>=0.9.0", "bluetooth-sensor-state-data>=1.7.0", "sensor-state-data>=2.18.0"]`, `dependencies: ["bluetooth_adapters"]`), 테스트·린트 의존성은 `requirements_test.txt`·`requirements_test_min.txt`·`requirements_lint.txt`로 분리 |
| DEP-07, CI-06 | PASS | 공통 `install.py`가 단일 resolver 호출 및 `pip check`, HA 구성요소(`bluetooth`, `bluetooth_adapters`, `usb`) 요구사항을 함께 설치하고 `ruff==0.17.1` 고정 |
| DEP-03, DEP-04, CI-09, CI-10 | PASS | `.github/workflows/update_dependencies.yml`이 공통 `dependency-update.yml`을 호출하며 `validation-command`로 `ruff`와 `pytest tests/components/omron/`을 실행. 로컬 `actions/update-manifest/update.py` dry-run 검증(`Requirements are current; no change`) 및 `.github/dependabot.yml`의 최소 환경 `exclude-paths` 설정 확인. 열린 Dependabot PR #250(`pytest-homeassistant-custom-component` 0.13.366→0.13.372, `expected-ha-version` 미갱신)은 자동 병합하지 않고 종료 후, `current` 환경을 `pytest-homeassistant-custom-component==0.13.373` 및 `expected-ha-version: 2026.10.2`로 일괄 갱신(최소 HA `2025.12.0` 환경은 보존) |
| DEP-08 | PASS | HACS 리뷰(hacs/default#10041)에 따라 코드가 직접 import하는 `sensor-state-data>=2.18.0`, `bluetooth-sensor-state-data>=1.7.0`을 `manifest.json`에 추가했고, `current` job의 `minimum` 모드가 두 하한을 설치해 검증 |
| HACS-01(구조), HACS-02 | PASS | 루트 `README.md`, `LICENSE`, `hacs.json` 및 `custom_components/omron` 구조 확인 |
| HACS-03, HACS-05 | PASS | HACS Custom 배지 사용, `validate / hacs` 및 `validate / hassfest` 통과, `custom_components/omron/brand/icon.png`(256×256)·`icon@2x.png`(512×512) 존재 |
| HACS-04 | PASS | 최신 GitHub 정식 Release 태그 `4.1.2`(`isPrerelease: false`)과 `custom_components/omron/manifest.json` `"version": "4.1.2"` 일치 |
| HACS-06 | PASS | GitHub 리포 description(`Omron BLE Home Assistant Integration`), topics, Issues/Discussions 활성화 및 `hacs.json` 유효 필드(`name`, `homeassistant`) 확인 |
| CI-01, CI-02 | PASS | `Tests`, `Validate`, `Update dependencies`가 공통 reusable workflow를 호출하며 `master` push, PR, `workflow_dispatch`, 주간 `schedule` 지원 |
| CI-03, CI-05 | PASS | 검증 workflow `permissions: contents: read`, `concurrency` 설정, 공통 workflow 전체 SHA 고정 |
| CI-04, CI-07, CI-12 | PASS | 필수 `ha` 테스트 job(`current`, `minimum-ha`)과 `lint` 병렬 실행, 오류 무시 래퍼 없음, `master` 브랜치 보호 미사용 확인 |
| CI-08 | PASS | 모든 workflow 파일에 비신뢰 PR 입력의 직접 shell 보간 없음, `pull_request_target` 미사용, 테스트에 secrets 불요, `secrets: inherit` 미사용 |
| CI-11 | N/A | 자동 릴리스 생성 workflow를 두지 않으며 수동 GitHub Release(`4.1.2`)로 배포 |
| DOC-01, DOC-06 | PASS | `README.md`를 표준 섹션 순서(소개 → 지원 모델 → 요구사항 → 설치·설정 → 빠른 시작 → 중요 제한사항 → 문서 → 지원 → 라이선스)로 정리하고 오류별 대응을 `docs/troubleshooting.md`로 이동·연결. `CONTRIBUTING.md`에 Core 대응 구조·실제 HA 테스트·의존성 절차·검증 환경 표 수록 |
| DOC-02 | PASS | `README.md`(영어)와 `README.ko.md`(한국어) 상호 전환 링크, 동일 배지·문서 목록·제한사항 유지 |
| DOC-03, DOC-04, DOC-05 | PASS | `sync_badges.py --check` 통과 (HACS → Release → HA minimum → Tests → Validate → License → 동적 Usage 순서) |
| DOC-07 | PASS | `README.md`·`README.ko.md`에서 공통 Bluetooth 문서(고정 SHA)와 `docs/troubleshooting.md` 연결, 기기 고유 깨우기·`-P-` 페어링·단일 앱 제약·진단 센서(`Last Failure`, `Duration`) 및 민감 정보 제거 후 제보 절차 안내 |
| REL-01, REL-02, REL-03, REL-05 | PASS | SemVer(`4.1.2`) 준수, 규칙 채택만을 위한 태그/릴리스 미생성, 최소 HA `2025.12.0` 유지, 채택·테스트 개편을 기능 릴리스와 분리 |
| REL-04 | N/A | 현행 유지보수 리포로 이전·중단 해당 없음 |

## 남은 작업

1. 현재 표준 SHA(`ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870`) 기준 필수 미충족·예외 없음. 실물 BLE 혈압계/체중계 하드웨어 검증은 별도 기기 보유 시 수행.

## 2026-10-23 Downloads 배지 부분 검증

- 기준: 중앙 `b54ac48102d6e326c6929071fdc02ba8fade794e`의 DOC-03/05·배지 생성기와 Validate 형식 검사기만 부분 채택. 대상 시작 SHA `535a5b2087ff467fa157dd282ebab3882fe68f7e`. 기존 전체 채택 결과·예외는 유지하며 이번 변경으로 전체 재채택 완료를 주장하지 않습니다.
- 계획/자체 리뷰: 양언어 관리 블록을 같은 중앙 렌더러로 생성, 잘못된 HACS 데이터 조회 배지를 Downloads로 교체, 집계 의미와 소스 파일 설치 제외를 양언어로 명시. GOV-02에 따라 중앙 호출 pin과 standards-ref를 같은 SHA로 맞춥니다. 이전 기준과 새 기준의 공용 workflow/action 파일은 동일하며 실행 설정·지원 최소·ZIP 배포 설정을 보존합니다.
- 검증 명령·결과와 최종 head SHA·원격 CI 링크는 이 PR 본문에 기록합니다. 문서·배지·형식 검사기 pin만 변경하므로 로컬 HA 환경을 재구축하지 않으며 실제 기기·HA UI·사용자 다운로드 집계는 미검증입니다.
- 실행 결과: WSL Ubuntu Python 3.14.4/PyYAML 6.0.3/packaging 26.0에서 중앙 `scripts/sync_badges.py <repo> --check`, `scripts/check_integration.py <repo>` 및 actionlint 1.7.12의 전체 caller workflow 검사 PASS. Windows Git의 `git diff --check` PASS. 양언어 배지 외 기존 본문은 집계 설명의 정정·추가만 허용하고 보존했습니다.
- 초기 검사 실패 기록: 단일 기준 SHA 계약에 맞지 않는 검사기 pin만의 갱신은 GOV-02에서 거부되어 모든 중앙 pin을 동일 SHA로 정합화했습니다. Windows 중앙 템플릿 CRLF 비교는 실패하여 LF 중앙 snapshot의 Linux 검사로 재검증했습니다. WSL Git은 Windows worktree gitdir 경로를 해석하지 못해 diff 검사를 실패했고 동일 변경의 Windows Git 검사로 대체했습니다. 실패를 PASS로 소급하지 않습니다.

## 2026-10-23 ZIP 릴리스 적용 검증

- 기준 `0c1592977591ecba077a4666d699840062711201` / 대상 시작 `6fe8a6f78e02f40fc9de6cb70dad10bf6a4ee19c`. HACS-04/CI-11 부분 갱신이며 기존 전체 채택 이력·예외는 유지합니다.
- 계획: hacs.json에 zip_release:true, filename:omron.zip을 추가하고 중앙 release.yml을 같은 SHA로 생성합니다. 기존 caller는 중앙 pin/standards-ref만 변경합니다. 지원 최소·통합 버전·의존성·구현·README 본문을 보존합니다.
- 검증: 중앙 생성기/정적 검사·actionlint·실제 packager로 추적된 통합 파일의 ZIP 루트·manifest/번역/brand 등 런타임 자료 포함과 저장소 메타데이터 제외를 확인합니다. CLI 검증 명령·실제 버전·최종 head SHA·ZIP 결과·독립 리뷰·원격 CI 링크는 이 PR 본문에서 한 번 기록합니다.
- 중앙 실행 로직은 독립 zip_release_review 리뷰에서 지적 수정 후 추가 지적 없음. 대상 caller/메타데이터/ZIP도 별도 독립 리뷰합니다. 로컬 HA 환경 재구축·새 릴리스 발행·asset 업로드·기기 설치는 수행하지 않으며 미실행을 PASS로 기록하지 않습니다. immutable-releases는 enabled:false로 확인했습니다.


## 2026-10-25 중앙 검사기 부분 갱신 검증

- 검토한 중앙 SHA: `1863343638014adb8a4001afc8fddc1a27dc500f`; 적용 시작 커밋: `e3a2277c5407f72ceaea7c12387c42107f2dd08a`.
- 범위: 중앙 pin·standards-ref·검토 기준 기록 정합화. HA 구현·요구 버전은 유지합니다.
- 로컬: Python 3.14.5 / PyYAML 6.0.3, `python scripts/check_integration.py <target>` (검토한 중앙 source에서 실행): PASS.
- actionlint: PASS. PR CI·독립 리뷰 결과는 PR 링크와 check 결과를 확인합니다. 이 로컬 결과만으로 전체 채택 완료를 주장하지 않습니다.
- 독립 리뷰: 별도 review_checker_rollout 담당자가 19개 리포의 workflow 69개·기존 job 100개를 구조 비교했습니다. 새 CI 회귀 지적 없음. 기존 job ID·입력·권한·지원 최소와 통합 코드 보존 및 새 standards job의 중앙 템플릿 일치 확인. 실행 변경 없는 이 기록 추가 전 작업 트리를 검토했습니다.
