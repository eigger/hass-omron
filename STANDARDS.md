# 적용한 공통 규칙

- 표준: https://github.com/eigger/hass-repo-standards/tree/main
- 참조 커밋 SHA: `0ec31111a81b0ee5702f1d8a17f03616a10a5db6`
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
