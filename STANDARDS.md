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
| TEST-02, TEST-03, TEST-09 | 60개 test 모듈 중 `hass` fixture·실제 config entry를 쓰는 모듈은 13개(`test_entry_platforms.py`, `test_entry_lifecycle_behavior.py` 추가). 두 모듈은 혈압계(HEM-7155T)·체중계(HN-300T2) 항목의 실제 setup → sensor/binary_sensor/button/text/number 로딩·상태·레지스트리, unload/reload/remove 정리, 광고·페어링 버튼·폴 데드라인과 unload 취소를 검증하고 BLE 경계(GATT 호출)만 대체한다. 남은 예외: 23개 모듈이 여전히 소스 텍스트·AST·`inspect.getsource`를 (일부) 사용하고(예: 폴 `CancelledError` 비삼킴·초기 폴 순서·bp5465 계약), 나머지는 프로토콜 단위 테스트(HA 미기동). 코어 stub(`sys.modules` 치환)은 없음 | @eigger | 남은 AST/소스 텍스트 모듈을 동작 테스트로 교체하는 후속 PR |
