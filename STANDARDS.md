# 적용한 공통 규칙

- 표준: https://github.com/eigger/hass-repo-standards/tree/main
- 참조 커밋 SHA: `8d4348ea951736054d08f7cfe9b19f6d44b37a5e` (2026-10-17 `git ls-remote`로 확인한 main)
- 채택일: 2026-10-17 (부분 채택 — 채택 PR 병합 전까지 진행 중)
- profile: integration / ha (실제 HA fixture·통합 로딩 필수)
- 적용 점검표: [ADOPTION_CHECKLIST.md](ADOPTION_CHECKLIST.md)
- 중앙 현황표: https://github.com/eigger/hass-repo-standards/blob/main/docs/adoption-status.md
- 조율 / 구현 / 독립 리뷰·검증 담당: 조율·구현 Claude Code 메인 세션 / 독립 리뷰 Opus 서브 에이전트
- 중앙 현황 갱신일: 미등록 (중앙 리포 접근·갱신은 이번 범위 밖)
- 실제 기본 브랜치: `master`
- 테스트 경로: `tests/components/omron/`
- 변경 전 지원 최소 HA / 선언 근거: 2025.12.0 / `hacs.json`
- 적용 후 지원 최소 HA: 2025.12.0 (변경 없음)
- current HA 테스트 환경: Python 3.14, pytest-homeassistant-custom-component 0.13.366 (HA 2026.9 설치; tests.yml 주석 근거)
- 최소 HA 검증 환경: 미검증 — 현재 CI는 최소 HA 2025.12.0에서 실행하지 않음
- 최소 package 검증: 미검증 (`blesession>=0.9.0` 하한 환경 없음)

## 예외

| 규칙 ID | 이유 | 담당자 | 재검토 조건 |
|---|---|---|---|
| TEST-02, TEST-03, TEST-09 | `conftest.py`가 homeassistant/bleak를 MagicMock으로 치환하는 테스트가 다수(58개 test 모듈 중 일부만 실제 HA 사용). 실제 HA fixture 전환은 별도 작업 | @eigger | 실제 HA 로딩 테스트 확대 PR |
| CI-01, CI-02 | 공통 reusable workflow 대신 로컬 workflow 유지(`blesession` 갱신 스크립트·`ha_component_requirements.py` 사용). 외부 action은 SHA 고정 | @eigger | 공통 workflow 전환 PR |
| TEST-04, HACS-01 | 최소 HA(2025.12.0) 전용 CI job 없음 | @eigger | 최소 HA용 plugin/Python 호환 조합 확인 시 |
| DEP-03, DEP-04, CI-09, CI-10 | 로컬 `update_blesession.py`가 `blesession>={latest}`로 재작성해 extras·marker를 보존하지 않고 major 정책이 없으며, 기본 브랜치 `master` 전용이고 HA 테스트 후 PR 동기화를 하지 않음 | @eigger | 공통 dependency-update 전환 시 |
