# 적용한 공통 규칙

- 표준: https://github.com/eigger/hass-repo-standards/tree/main
- 참조 커밋 SHA: `0c1592977591ecba077a4666d699840062711201`
- 기준 확인: 2026-10-21 `git ls-remote`로 확인한 main, 공통 workflow/action 호출 SHA와 동일
- 채택일: 2026-10-18
- profile: integration / ha (실제 HA fixture·통합 로딩 필수)
- 적용 점검표: [ADOPTION_CHECKLIST.md](ADOPTION_CHECKLIST.md)
- 중앙 현황표: https://github.com/eigger/hass-repo-standards/blob/main/docs/adoption-status.md
- 조율 / 구현 / 독립 리뷰·검증 담당: 조율 `root` / 구현 `docs_implementation`, `readme_implementation` / 독립 리뷰·검증 `independent_review`
- 중앙 현황 갱신일: 2026-10-21 (로컬 현황표 갱신, 원격 미반영)
- 실제 기본 브랜치: `master`
- 테스트 경로: `tests/components/omron/`
- 변경 전 지원 최소 HA / 선언 근거: 2025.12.0 / `hacs.json`
- 적용 후 지원 최소 HA: 2025.12.0 (변경 없음)
- current HA 테스트 환경: Python 3.14, pytest-homeassistant-custom-component 0.13.373, HA 2026.10.2 (`expected-ha-version`로 CI 검증; 2026-10-21 로컬 Python 3.14.5 / HA 2026.10.2 / plugin 0.13.373 검증)
- 최소 HA 검증 환경: CI `minimum-ha` job (Python 3.13, HA 2025.12.0, plugin 0.13.298). 2026-10-21 로컬 독립 검증 Python 3.13.14 / 동일 HA·plugin 통과. 과거 원격 CI와 이번 로컬 결과는 체크리스트에서 구분
- 최소 package 검증: CI `current` job의 `minimum` 모드 및 2026-10-21 로컬 HA 2026.10.2에서 `blesession==0.9.0`, `bluetooth-sensor-state-data==1.7.0`, `sensor-state-data==2.18.0` 통과

## 예외

현재 등록된 예외 없음 (기존 `TEST-02, TEST-03, TEST-09` 예외였던 Python 소스 텍스트·AST·`inspect.getsource` 검사 23개 모듈을 모두 실제 HA 통합 및 런타임 동작 테스트로 전환 완료).

## 2026-10-21 재적용

- 기존 기준 `ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870`에서 최신 기준으로 이관. 공통 Tests·Validate·의존성 갱신 caller를 고정 SHA로 갱신하고 `standards-ref`와 중앙 template compliance job을 적용.
- HACS Default: [공식 integration 목록](https://github.com/hacs/default/blob/master/integration)에 `eigger/hass-omron` 등재를 2026-10-21 확인. Default 배지·공식 HACS Downloads·My Home Assistant 설치 배지 및 검색 설치 안내 적용. 다운로드 수는 활성 설치 수가 아님.
- HA-02: 실제 지원 제품이 혈압계·체중계·체성분계이므로 `categories: ["health_wellbeing"]` 채택. 플랫폼 종류에서 분류를 추정하지 않았으며 `.hass-standards.json.categories_review`에 근거 기록.
- README 공통 영역은 중앙 renderer로 생성. 기존 영어 모델표·설정·예제 보존, 한국어 전체 정보 보완. blesession 양언어 사용자 원본과 기기별 문제 해결 문서 연결.
- 테스트 기반은 중앙 exact 파일/관리 fixture 블록 및 Core 대응 파일명으로 정리. 실제 동작 회귀 범위를 유지하며 최소 HA용 event-loop fixture는 domain conftest에 보존.
- HA-11: `strings.json`의 미해결 Core 참조를 기존 영어 배포 번역으로 명시하고 한국어에 누락된 기기 선택/중복/재인증 문구를 보완.
- 조율 `root`, 문서 구현 `docs_implementation`, README·테스트 구조 구현 `readme_implementation`, 독립 리뷰·최소 HA 검증 `independent_review`.
- 지원 최소 HA 2025.12.0, 통합 버전 4.1.2, 런타임 의존성 하한 유지. 재적용 검증은 로컬 작업 트리에서 수행했으며, 사용자 요청으로 PR을 제출합니다. 새 릴리스는 만들지 않으며 원격 CI 결과는 제출 후 별도로 확인해야 합니다. 실제 실행 결과는 체크리스트 재적용 절 참조.

## PR #254 CI 후속 수정

- 최초 Validate [run 39598680526](https://github.com/eigger/hass-omron/actions/runs/39598680526)의 Hassfest는 published 이미지의 이전 schema가 `categories`를 거부해 실패. 같은 실행의 HACS·중앙 템플릿 검사는 통과했으며, [Tests run 39598680489](https://github.com/eigger/hass-omron/actions/runs/39598680489)는 current/minimum-library/minimum-HA·lint 전부 통과.
- 중앙 CI 계약에 따라 `hassfest-core-ref: ec18d8b25d1ee39aa8b85a72a170e42bee33228c`를 명시. 해당 upstream Core의 manifest category 지원과 수정하지 않은 공식 Dockerfile을 확인했고, 이전 로컬 Hassfest 통과 snapshot과 동일한 기준을 CI에 적용. 지원 최소 HA·통합 manifest·분류를 유지.
- 공식 published 이미지가 해당 schema를 지원하면 같은 통합 변경을 기본 이미지에서 실제 검증한 뒤 입력을 제거. source fetch/build/전체 Hassfest 실패는 그대로 job 실패이며 검사를 생략하지 않음.

## 2026-10-23 다운로드 배지 부분 갱신

- 배지·DOC-03/05와 Validate 형식 검사기만 중앙 [`b54ac48102d6e326c6929071fdc02ba8fade794e`](https://github.com/eigger/hass-repo-standards/tree/0c1592977591ecba077a4666d699840062711201)를 채택합니다. 이전 기준 SHA `0ec31111a81b0ee5702f1d8a17f03616a10a5db6`의 채택 이력·예외는 보존합니다. 모든 중앙 workflow/action pin과 standards-ref는 GOV-02의 단일 기준 SHA 계약에 맞춰 갱신합니다. 중앙 diff에서 공용 workflow/action 실행 파일은 이전 기준과 동일함을 확인했습니다.
- 대상 기본 브랜치 커밋: `535a5b2087ff467fa157dd282ebab3882fe68f7e`. 주 에이전트 직접 구현·자체 리뷰(GOV-05의 배지/생성물 변경 범위), 검증은 [ADOPTION_CHECKLIST.md](ADOPTION_CHECKLIST.md#2026-10-23-downloads-배지-부분-검증)에 기록합니다.
- Downloads는 모든 릴리스 첨부 asset 다운로드 합계입니다. 사용자 지시에 따라 ZIP 배포를 도입하지 않으며 현재 HACS 소스 파일 설치는 이 수치에 집계되지 않습니다. 통합 코드·의존성·지원 최소·기존 설치 안내는 변경하지 않습니다.

## 2026-10-23 ZIP 릴리스 배포

- 기준 중앙 SHA: `0c1592977591ecba077a4666d699840062711201` ([중앙 PR15](https://github.com/eigger/hass-repo-standards/pull/15) 스쿼시 머지). 이전 `b54ac48102d6e326c6929071fdc02ba8fade794e` 채택 이력·예외를 보존하며 HACS-04·CI-11의 ZIP 배포 계약을 추가합니다.
- 대상 시작 SHA: `6fe8a6f78e02f40fc9de6cb70dad10bf6a4ee19c`. hacs.json의 zip_release/filename과 중앙 release caller를 함께 적용합니다. archive 파일명은 `omron.zip`, ZIP 루트는 해당 통합 파일입니다.
- 중앙 workflow/action pin과 standards-ref는 단일 SHA로 정합화합니다. release:published에서 기존 해당 릴리스에 ZIP만 첨부하며 소스 버전·태그·릴리스 발행은 자동 변경하지 않습니다. 기존 asset 덮어쓰기를 거부합니다.
- 기존 지원 최소 HA `2025.12.0`와 통합 버전 `4.1.3` 및 의존성·구현은 보존합니다. 기존 태그의 hacs.json은 소급 수정하지 않으므로 다음 릴리스부터 ZIP 설치로 전환됩니다.
- immutable-releases 설정은 enabled=false/enforced_by_owner=false로 확인했습니다. 업로드 지연·실패 및 기존 asset 복구는 중앙 [릴리스 계약](https://github.com/eigger/hass-repo-standards/blob/0c1592977591ecba077a4666d699840062711201/docs/releases.md#zip-릴리스-첨부)을 따릅니다. 실제 새 릴리스 발행·설치·기기 검증은 이번 작업에 포함하지 않습니다.
