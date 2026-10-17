# 적용한 공통 규칙

- 표준: https://github.com/eigger/hass-repo-standards/tree/main
- 참조 커밋 SHA: `ff46b0c8d9ba53b1d35ab9a4aa38f4db26c06870` (2026-10-17 `git ls-remote`로 확인한 main, 공통 workflow 호출 SHA와 동일)
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
- current HA 테스트 환경: Python 3.14, pytest-homeassistant-custom-component 0.13.366, HA 2026.9.3 (`expected-ha-version`로 CI 검증)
- 최소 HA 검증 환경: CI `minimum-ha` job (Python 3.13, HA 2025.12.0, plugin 0.13.298). 원격 CI 결과는 체크리스트 참조
- 최소 package 검증: CI `current` job의 `minimum` 모드로 blesession==0.9.0 (원격 CI 결과 참조)

## 예외

| 규칙 ID | 이유 | 담당자 | 재검토 조건 |
|---|---|---|---|
| TEST-02, TEST-03, TEST-09 | `conftest.py`가 homeassistant/bleak를 MagicMock으로 치환하는 테스트가 다수(58개 test 모듈 중 일부만 실제 HA 사용). 실제 HA fixture 전환은 별도 작업 | @eigger | 실제 HA 로딩 테스트 확대 PR |
