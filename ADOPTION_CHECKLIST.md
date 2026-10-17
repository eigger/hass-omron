# 공통 규칙 적용 체크리스트

[사용법과 판정 기준](https://github.com/eigger/hass-repo-standards/blob/8d4348ea951736054d08f7cfe9b19f6d44b37a5e/docs/checklist.md). 상태: `PASS` 근거 확인, `TODO` 미충족/미확인, `EXCEPTION` [STANDARDS.md](STANDARDS.md) 예외, `N/A` 해당 없음. 최초 검토이며 모든 항목은 TODO로 시작해 근거가 있는 것만 변경했습니다.

## 검토 대상

| 항목 | 값 |
|---|---|
| 리포 / 기본 브랜치 | eigger/hass-omron / master |
| domain / 구현 경로 | omron / custom_components/omron |
| 검토 대상 통합 커밋 | 601ad6a + 미커밋 변경 (채택 PR 없음) |
| 검토한 표준 SHA / 검토일 | 8d4348ea951736054d08f7cfe9b19f6d44b37a5e / 2026-10-17 |
| 구현 / 독립 리뷰·검증 | 메인 세션 / Opus 서브 에이전트 1차 리뷰 완료(지적 반영 후 재검증 미실시) |
| 중앙 현황표 | 미등록 — 반영할 내용: 진행 중, 부분 채택, 테스트 구조 이관 남음 |
| 지원 최소 HA | 2025.12.0 (hacs.json) — 변경 없음 |
| profile / 테스트 경로 | integration / ha, `tests/components/omron/` |
| 채택 판정 | 이관 진행 중 (부분 채택) |

## 검증 환경

| 축 | 통합 커밋 | Python / HA / 플러그인 | 대상 패키지 | 명령·결과 | 미검증 범위 |
|---|---|---|---|---|---|
| 현재 / PASS | 601ad6a + 미커밋 변경 | 3.14.5 / HA 2026.9.3 / pytest-homeassistant-custom-component 0.13.366 (로컬 venv) | blesession 0.9.0 | `ruff check custom_components/` 통과, `pytest tests/` 599 passed, `pip check` 이상 없음 | 원격 CI 미실행 |
| 정확한 최소 HA 2025.12.0 / TODO | 미확인 | 미확인 | 미확인 | 미실행 | 전체 |
| 정확한 최소 라이브러리 (blesession 0.9.0 하한) / TODO | 미확인 | 미확인 | 미확인 | 로컬은 우연히 0.9.0이 설치됨, 별도 최소 환경으로는 미검증 | 전체 |

## 항목

| 규칙 ID | 상태 | 근거 / 남은 작업 |
|---|---|---|
| GOV-01 | PASS | STANDARDS.md에 SHA·profile·브랜치·예외 기록 |
| GOV-02 | EXCEPTION | 공통 workflow 미사용(외부 action만 SHA 고정). STANDARDS.md 예외 |
| GOV-03 | TODO | 채택 PR 필요 |
| GOV-04 | TODO | 중앙 표 미갱신 |
| GOV-05 | TODO | Opus 1차 리뷰 완료, 지적 수정 후 재검증 필요 |
| HA-01, HA-13 | TODO | domain 일치 확인(manifest `omron`), Core 대응 구조 점검 안 함 |
| HA-02 | PASS | manifest 키 순서(domain, name, 이후 알파벳) 확인 `custom_components/omron/manifest.json` |
| HA-03~HA-12 | TODO | 이번 범위에서 코드 점검하지 않음 |
| TEST-08 | PASS | 58개 test 모듈을 `tests/components/omron/`로 이동 (git mv), `pytest` 599 passed (Python 3.14, HA 2026.9.3) |
| TEST-02, TEST-03, TEST-09 | EXCEPTION | conftest가 homeassistant/bleak를 MagicMock으로 치환. STANDARDS.md 예외 |
| TEST-01, TEST-06, TEST-07 | TODO | 미점검 (599 passed는 확인) |
| TEST-04, HACS-01 | EXCEPTION | 최소 HA job 없음 |
| TEST-05, HA-10, DEP-02, DEP-05 | TODO | 최소 blesession 별도 환경 없음 |
| DEP-01, DEP-06 | PASS | manifest requirements=blesession, dependencies=bluetooth_adapters, 테스트 requirements 분리 |
| DEP-07, CI-06 | TODO | CI에 `pip check` 없음, ruff 미고정(`tests.yml`), pip 호출 분산. 로컬 `pip check`만 통과 |
| DEP-02, DEP-03, DEP-08 | TODO | 이번 변경은 의존성 변경 없음, 업데이터 갭은 예외 참조 |
| DEP-03, DEP-04, CI-09, CI-10 | EXCEPTION | STANDARDS.md 예외 |
| HACS-01(구조), HACS-02 | PASS | 루트 README/LICENSE/hacs.json 존재 |
| HACS-03, HACS-05 | TODO | brand/icon.png 존재, HACS/Hassfest 실행 결과 미확인 |
| HACS-06 | TODO | 미점검 |
| CI-01, CI-02 | EXCEPTION | 로컬 workflow 유지 |
| CI-03, CI-05 | PASS(부분) | permissions·persist-credentials false·timeout·concurrency 적용, action SHA 고정. hacs/action·hassfest는 브랜치 SHA라 수동 갱신 필요 |
| CI-04, CI-07, CI-12 | TODO | 원격 CI 미실행, required checks 이름은 변경 없음 |
| CI-08 | TODO | 비신뢰 입력·secrets 경계 미점검 |
| DOC-01, DOC-06 | PASS(부분) | 상세 참조를 docs/entities.md로 이동, CONTRIBUTING 추가. README의 오류별 대응·Important Notice YAML은 메인에 남음 |
| DOC-02 | PASS | README.md 영어 / README.ko.md 한국어 상호 링크, 기존 내용 보존 |
| DOC-03, DOC-04, DOC-05 | PASS | `sync_badges.py --check` 통과 |
| HA-09, HA-10 | TODO | 미점검 |
| DOC-07 | TODO | 공통 Bluetooth 문서 링크 미추가 (docs/troubleshooting.md는 존재) |
| HA-12 | TODO | 로그/diagnostics 비식별화 미점검 |
| HACS-04, REL-01~REL-03, CI-11 | TODO | 이번 변경은 릴리스하지 않음. migration 해당 여부 미확인 |
| REL-04, REL-05 | TODO | 미점검 |

## 남은 작업

1. conftest의 HA MagicMock 치환을 실제 HA fixture·config entry 로딩 테스트로 전환(별도 PR).
2. 최소 HA 2025.12.0 / 최소 blesession 검증 job, CI에 `pip check`·ruff 고정.
3. 공통 workflow·dependency-update 전환, DOC-07 Bluetooth 문서 연결.
4. 원격 CI 확인, 중앙 표 갱신, 리뷰 지적 재검증.
