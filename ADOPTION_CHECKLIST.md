# 공통 규칙 적용 체크리스트

[사용법과 판정 기준](https://github.com/eigger/hass-repo-standards/blob/ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870/docs/checklist.md). 상태: `PASS` 근거 확인, `TODO` 미충족/미확인, `EXCEPTION` [STANDARDS.md](STANDARDS.md) 예외, `N/A` 해당 없음. 최초 검토이며 모든 항목은 TODO로 시작해 근거가 있는 것만 변경했습니다. 한 행에 묶은 규칙은 결과가 같은 것만 묶었습니다.

## 검토 대상

| 항목 | 값 |
|---|---|
| 리포 / 기본 브랜치 | eigger/hass-omron / master |
| domain / 구현 경로 | omron / custom_components/omron |
| 채택 PR | [#249](https://github.com/eigger/hass-omron/pull/249) (브랜치 chore/adopt-shared-standards) |
| 검토한 표준 SHA / 검토일 | ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870 (공통 workflow 호출 SHA와 동일) / 2026-10-17 |
| 구현 / 독립 리뷰·검증 | 메인 세션 / Opus 서브 에이전트 (2회 리뷰, 지적 반영) |
| 중앙 현황표 | 미등록 — 반영할 내용: 진행 중, 부분 채택, 실제 HA 로딩 테스트 전환 남음 |
| 지원 최소 HA | 2025.12.0 (hacs.json) — 변경 없음 |
| profile / 테스트 경로 | integration / ha, `tests/components/omron/` |
| 채택 판정 | 부분 채택 — 필수 TODO(위 항목)와 예외 1건 남음 |

## 검증 환경

| 축 / 상태 | 통합 커밋 | Python / HA / 플러그인 | 대상 패키지 | 명령·결과 | 미검증 범위 |
|---|---|---|---|---|---|
| 로컬 current / PASS | 43af360 | 3.14.5 / HA 2026.9.3 / 0.13.366 | blesession 0.9.0 | `ruff check .`, `ruff format --check .` 통과, `pytest tests/components/omron` 599 passed, `pip check` 이상 없음 | |
| CI current / PASS | 43af360 | 3.14 / 2026.9.3 / 0.13.366 | blesession 최신 | `current / Python 3.14 / ha / current` pass (run 39039311253) | |
| CI 최소 라이브러리 / PASS | 43af360 | 3.14 / 2026.9.3 / 0.13.366 | blesession==0.9.0 | `current / Python 3.14 / ha / minimum` pass (run 39039311253) | |
| CI 최소 HA 2025.12.0 / PASS | 43af360 | 3.13 / 2025.12.0 / 0.13.298 | blesession>=0.9.0 | `minimum-ha / Python 3.13 / ha / current` pass (run 39039311253). 이전 실패 원인: plugin의 event loop fixture, dbus-fast 3.1.2의 `MessageBus.__init__` 소켓, probatio 미설치 — 테스트 수정으로 해결 | 최소 HA 로컬 미실행 |

## 항목

| 규칙 ID | 상태 | 근거 / 남은 작업 |
|---|---|---|
| GOV-01 | PASS | STANDARDS.md에 SHA·profile·브랜치·예외 기록 |
| GOV-02 | PASS | 공통 workflow 호출을 ff46b0c 전체 SHA로 고정 (`.github/workflows/*.yml`) |
| GOV-03 | TODO | 채택 PR #249 병합 전 |
| GOV-04 | TODO | 중앙 표 미갱신 (중앙 리포 수정은 이번 범위 밖) |
| GOV-05 | PASS | 구현 후 별도 Opus 서브 에이전트가 2회 독립 리뷰, 지적(false 서술·CI 실패 등) 수정. 최종 재검증은 PR CI |
| HA-01, HA-13 | TODO | domain 일치 확인(manifest `omron`), Core 대응 구조 점검 안 함 |
| HA-02 | PASS | manifest 키 순서(domain, name, 이후 알파벳) 확인, 공식 품질 등급 표기 없음 |
| HA-03~HA-08, HA-11, HA-12 | TODO | 이번 범위에서 코드 점검하지 않음 |
| HA-09 | TODO | BLE 경계·proxy 경로 점검 안 함 |
| HA-10 | TODO | 최소 blesession 환경은 CI에서 통과했으나 API 경계 검토는 미실시 |
| TEST-08 | PASS | 58개 test 모듈을 `tests/components/omron/`로 이동, 599 passed |
| TEST-02, TEST-03, TEST-09 | EXCEPTION | `hass` 사용 모듈은 약 11개, 약 23개는 소스·AST 검증. STANDARDS.md 예외 |
| TEST-01, TEST-06, TEST-07 | TODO | 기능별 회귀 범위 점검 안 함 |
| TEST-04, HACS-01(최소 검증) | PASS | `minimum-ha` job이 HA 2025.12.0(expected-ha-version 검증)에서 599개 통과, 43af360 |
| TEST-05, DEP-02, DEP-05 | PASS | `current / … / minimum`에서 blesession==0.9.0 설치(공통 install.py가 설치값 검증)·테스트 통과 |
| DEP-01, DEP-06 | PASS | manifest requirements=blesession, dependencies=bluetooth_adapters, 테스트 requirements 분리 |
| DEP-07, CI-06 | PASS | 공통 install.py가 `pip check`·HA 구성요소(bluetooth, bluetooth_adapters, usb) requirements를 설치, ruff 0.17.1 고정(requirements_lint.txt). current·minimum-ha job 통과 |
| DEP-03, DEP-04, CI-09, CI-10 | TODO | 공통 dependency-update 호출(`update_dependencies.yml`)로 전환. 실행·dry-run 비교는 미실시 |
| DEP-08 | TODO | 이번 변경에 의존성 추가 없음. 업데이트 PR 시 확인 |
| HACS-01(구조), HACS-02 | PASS | 루트 README/LICENSE/hacs.json 존재 |
| HACS-03, HACS-05 | PASS | `validate / hacs`, `validate / hassfest` pass (df7b9bc), brand/icon.png 존재 |
| HACS-06 | TODO | 미점검 |
| CI-01, CI-02 | PASS | Tests/Validate/Update dependencies가 공통 workflow 호출, push(master)·PR·dispatch·schedule |
| CI-03, CI-05 | PASS | 호출 workflow permissions·concurrency, 공통 workflow의 SHA 고정·persist-credentials false·timeout |
| CI-04, CI-07, CI-12 | PASS | 필수 `ha` 테스트 job(current, minimum-ha)과 lint가 43af360에서 통과. 체크 이름이 current/minimum-ha/validate로 변경, master에 branch protection 없음(gh api 404) |
| CI-08 | TODO | 비신뢰 입력·secrets 경계 미점검 |
| DOC-01, DOC-06 | TODO | 상세 참조는 docs/entities.md로 분리, CONTRIBUTING 추가. README에 오류별 대응·Important Notice YAML이 남아 있어 완전 분리는 미완 |
| DOC-02 | PASS | README.md 영어 / README.ko.md 한국어 상호 링크, 기존 내용 보존, 문서 목록 동일 |
| DOC-03, DOC-04, DOC-05 | PASS | `sync_badges.py --check` 통과 (Usage 포함) |
| DOC-07 | PASS | README(영/한)에서 공통 Bluetooth 안내 SHA 링크, docs/troubleshooting.md에 모델 고유 조건·진단 경로·제보 절차. 실물 검증 주장 없음 |
| HACS-04, REL-01~REL-03, CI-11 | TODO | 이번 변경은 릴리스하지 않음. migration 해당 여부 미확인 |
| REL-04, REL-05 | TODO | 미점검 |

## 남은 작업

1. `hass` fixture·config entry 기반 테스트 확대(TEST-02/03/09 예외 해소, 별도 PR).
2. 의존성 업데이트 workflow 첫 실행 확인, 중앙 표 갱신.
