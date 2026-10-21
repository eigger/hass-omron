# 문제 해결

[README](../README.ko.md) | [English](troubleshooting.md) | **한국어**

혈압계가 동기화되지 않으면 통합에 원인이 이미 기록되어 있는 경우가 많습니다. 마지막 BLE 세션의 세부 정보를 담은 두 진단 센서부터 확인하고, 센서에 드러나지 않는 경우와 이슈에 첨부할 자료를 살펴보세요.

## 공통 문제 해결

먼저 [공통 Bluetooth 문제 해결](https://github.com/eigger/blesession/blob/main/docs/troubleshooting.ko.md)을 확인하세요. 프록시 선택·펌웨어·Home Assistant 등록은 [Bluetooth proxy 안내](https://github.com/eigger/blesession/blob/main/docs/bluetooth.ko.md)를 따르세요.

## 기기별 확인 사항

공통 프록시 설정, 권장 하드웨어, 능동 스캔과 GATT 연결의 차이, 일반적인 연결 점검은 [Bluetooth proxy 안내](https://github.com/eigger/blesession/blob/main/docs/bluetooth.ko.md)에서 관리합니다. 다음은 Omron 혈압계·체중계의 조건입니다.

- **기기가 보이는 것과 연결되는 것은 다릅니다.** 광고(검색, RSSI)를 수신해도 GATT 연결·페어링·메모리 읽기는 실패할 수 있습니다. **Last Failure**의 `failed_stage`로 구분하세요. `connect`는 연결, `session`/`auth`는 서비스와 페어링, `transfer`는 기록 읽기입니다.
- **깨우기 / 전원.** 기기는 절전을 위해 Bluetooth 무선을 끕니다. 측정 직후 잠시 연결할 수 있습니다. 페어링할 때는 Bluetooth 버튼을 길게 눌러 `-P-`가 깜박이게 하고, 깜박이는 동안 제출하세요.
- **한 번에 하나의 페어링된 휴대폰/앱.** 공식 Omron Connect 앱이나 다른 호스트가 페어링을 보유하고 있다면 먼저 해제하세요. 그렇지 않으면 신호가 강해도 `auth` 단계가 실패할 수 있습니다.
- **프록시 경로.** Home Assistant가 사용 가능한 Bluetooth 어댑터와 ESPHome 프록시 중에서 선택합니다(**Duration** 속성의 무선 경로 확인). `bluetooth_proxy`의 `active: true`가 필요하며 스캔 간격은 기본값을 유지하세요. 프록시를 기기 가까이에 두고 제보에 **Duration**의 무선 경로를 포함하세요.
- **미검증 모델.** README에서 체크 표시가 있는 모델만 검증되었습니다. 나머지를 “유사한 모델”로 선택한 경우에는 미검증입니다.
- **제보.** 디버그 로그와 센서 속성을 첨부하되, 주소·본딩 키·공개하고 싶지 않은 측정값을 먼저 제거하세요([Bluetooth 로그 수집](capturing-bluetooth-logs.md), 영어).
- **전송 조건.** 기록을 읽으려면 기기가 깨어 있어야 하며, 유효한 본딩/전송 자격 증명과 일치하는 모델 프로필이 필요합니다. 아래의 재시도 횟수와 180초 세션 제한은 Omron 통합 정책입니다. 측정 통합이므로 이미지 전송 간격이나 payload 설정은 해당하지 않습니다. 검증 보고가 없는 모델·펌웨어·로컬 어댑터/프록시 조합은 미검증입니다.

## 증상과 조치

| 증상 | 실패 단계 | 확인 사항 | 조치 |
|---|---|---|---|
| 기기가 보이지만 연결되지 않음 | `connect` / `settle` | 깨어 있는 시간, `rssi`, `paths`, 프록시 슬롯과 본딩을 보유한 무선 장치 | 새로 측정하고, 가까이 옮기거나 슬롯을 확보하거나 사용할 무선 장치를 통해 다시 페어링하세요. |
| 연결됐지만 필요한 서비스가 없음 | `session` (`services`) | 설정 모델과 프록시 GATT 캐시 | 올바른 모델을 선택하고, 캐시 복구로 해결되지 않으면 프록시를 재시작하세요. |
| 페어링 또는 잠금 해제 실패 | `auth` (`pair`, `unlock`, `memory_open`) | 깜박이는 `-P-`, 저장된 자격 증명, 다른 호스트, `memory_session_attempts` | 다른 앱의 연결을 중단하고 페어링 가능 시간 안에 다시 페어링하세요. |
| 기록 읽기 도중 중단 | `transfer` (`readout`) | `rssi`, `via`, 항상 같은 위치에서 실패하는지 | 배치를 개선하고, 모델별로 일관되게 실패하면 민감 정보를 제거한 캡처와 함께 제보하세요. |
| 쓰기 호출이 반환되지 않음 | `failed_stage` / `failed_detail`의 단계; `WriteTimeout` | 정확한 `error`와 무선 장치/프록시 상태 | 반복하기 전에 어댑터/프록시 상태를 확인하세요. 재페어링으로 멈춘 전송 경로가 복구되지는 않습니다. |
| 기록을 읽었지만 종료 실패 | `finish` (`memory_close`) / `disconnect` | 기록 도착 여부와 다음 세션 성공 여부 | 다음 폴링을 관찰하고, 이후 잠금 해제/세션 열기가 실패하면 다시 페어링하세요. |
| 세션은 성공했지만 새 측정값이 없음 | 실패 없음; `records` | 저장 데이터, 사용자 슬롯, 이미 표시된 기록인지 | 원하는 사용자를 선택한 뒤 새로 측정하세요. |
| Duration이 바뀌지 않거나 버튼이 즉시 실패 | BLE 세션 시작 전 | 광고 수신 가능 여부와 Connection 상태 | 기기를 깨운 뒤 Refresh Data를 누르세요. 세션이 진행 중이면 끝난 뒤 페어링을 재시도하세요. |

### 실패 읽기

Last Failure의 `failed_stage`부터 확인하세요. 공통 명칭으로 어느 단계까지 진행했는지 알려줍니다. `failed_detail`은 해당 단계의 기기별 명칭입니다. `likely_cause`는 단계·실패 유형·무선 상황을 바탕으로 해석한 원인이며 `error`는 정확한 메시지입니다. `likely_cause_key`가 있으면 그 해석의 고정 식별자입니다.

#### `connect`

연결이 성립하지 않았습니다.

- *측정 사이에는 정상적인 상태입니다.* 절전을 위해 무선을 끄고, 측정 직후와 Bluetooth 기호가 표시되는 동안 잠시 깨어납니다. 다시 잠든 직후의 폴링은 여기서 실패하고 다음 측정 직후에는 성공할 수 있습니다. 동기화가 **한 번도 성공하지 않을 때** 더 조사하세요.
- *확인:* `rssi`, `paths`. `failed_detail: settle` 또는 `error`에 *settle*이 있으면 연결을 수락한 뒤 암호화가 안정되기 전에 끊은 것입니다. *slot*이 있으면 프록시 연결 슬롯이 모두 사용 중입니다.
- *조치:* `rssi`가 약하면(약 −85 dBm 이하는 배치 참고값이며 모든 환경에 적용되는 기준은 아닙니다) 기기를 옮기거나 가까이에 프록시를 추가하세요. 여러 프록시에서 *settle*이 발생하면 페어링한 무선 장치만 본딩을 보유하므로 `via`와 `advertised_via`를 확인하고 현재 사용할 프록시를 통해 다시 페어링하세요. *slot*이면 프록시당 BLE 기기를 줄이거나 프록시를 추가하세요.

#### `session` (`failed_detail: services`)

연결됐지만 모델 프로필에서 기대하는 GATT 서비스가 없습니다.

- *확인:* 항목에 설정한 모델과 기기 라벨이 같은지 확인하세요.
- *조치:* 모델이 틀리면 항목을 삭제하고 올바른 모델로 다시 추가하세요. 모델이 맞으면 프록시의 오래된 GATT 캐시일 수 있습니다. 통합이 캐시를 비우고 재시도하지만 계속 실패하면 프록시를 재시작하세요.

#### `auth` (`failed_detail: pair`)

본딩이 실패했습니다(`operation: pairing`).

- *확인:* *Could not enter key programming mode*는 기기가 페어링 모드가 아니었다는 뜻입니다.
- *조치:* Bluetooth 버튼을 길게 눌러 **-P-**가 깜박이게 하고 그동안 *Retry Pairing*을 누르세요.

#### `auth` (`failed_detail: unlock`)

애플리케이션 수준 잠금 해제를 거부했거나 응답하지 않았습니다.

- *확인:* *pairing key mismatch*는 저장 키를 더 이상 수락하지 않는다는 뜻입니다. *No stored transport credential*은 해당 모델에 필요한 자격 증명이 없다는 뜻입니다. *PIN or Key Missing* / *auth*는 저장된 **본딩**을 수락하지 않는다는 뜻입니다.
- *조치:* 다른 호스트(휴대폰 앱)가 페어링을 가져갔거나, 프록시 재설치·어댑터 변경으로 이쪽 본딩을 잃은 경우가 많습니다. 기기를 **-P-** 상태로 두고 *Retry Pairing*을 누르세요. 며칠마다 다시 페어링해야 한다면 그사이에 휴대폰이 다시 페어링하는지 확인하세요.

#### `auth` (`failed_detail: memory_open`)

잠금은 해제했지만 기록 읽기 세션 시작을 거부했습니다.

- *확인:* 반복되는지 보세요. `memory_session_attempts: 3`은 모든 시도가 실패했다는 뜻입니다.
- *조치:* 한 번이면 다음 폴링에서 대개 성공합니다. 매번 발생하면 `unlock`과 마찬가지로 다시 페어링하세요.

#### `transfer` (`failed_detail: readout`)

기록 메모리를 읽는 중 실패했습니다. 연결 품질이나 메모리 맵 문제를 살펴볼 단계입니다.

- *확인:* `rssi`, `via`, 매번 같은 위치에서 실패하는지 확인하세요.
- *조치:* 일회성이면 기기나 사용한 프록시(`via`)를 옮기거나 프록시를 추가하세요. 미검증 모델에서 매번 발생하면 모델 메모리 맵이 잘못됐을 수 있으므로 [이슈](https://github.com/eigger/hass-omron/issues)에 Bluetooth 로그를 첨부하세요([로그 수집](capturing-bluetooth-logs.md), 영어).

#### `finish` (`failed_detail: memory_close`) / `disconnect`

기록은 읽었고 종료만 실패했습니다. 이것만으로는 문제가 되지 않습니다. 하지만 **다음** 연결 직후 `unlock` 또는 `memory_open`이 실패하면 기기가 세션을 확정하지 못한 것이므로 다시 페어링하세요.

#### 빠른 확인

- **`Pairing failed: Could not enter key programming mode`** — 제출 전에 `-P-`가 실제로 깜박이는지 확인하세요. 시간이 지나 꺼졌다면 버튼을 길게 눌러 다시 진입하세요.
- **`Connection terminated by peer`** — BLE 간섭이나 약한 신호일 수 있습니다. Home Assistant Bluetooth 어댑터/프록시 가까이 옮기거나 동글에 USB 연장 케이블을 사용하세요.
- **센서 값이 갱신되지 않음** — 기기는 절전을 위해 무선을 끄고 측정 직후 잠깐 켭니다. 페어링 후 *새로운* 측정으로 동기화를 시험하세요.
- **배터리 교체 후 데이터가 나타나지 않음** — 내부 시계가 초기화될 수 있습니다. 구형 모델은 재페어링 또는 앱에서 시각 동기화 후 HA와 재페어링이 필요할 수 있습니다.
- **`error: Session deadline reached`** — 세션이 멈춰 3분 제한에서 중단됐습니다. `failed_stage`가 위치를 알려줍니다. 멈춘 `bluetoothd`나 세션 중 꺼진 프록시가 원인일 수 있지만 시간 제한만으로 원인을 확정할 수는 없습니다. 반복되면 어댑터/프록시를 재시작하세요.
- **`rssi`가 낮지만 `paths`가 2 이상** — 다른 무선 장치가 더 나을 수 있습니다. Home Assistant는 가장 강한 광고를 통해 연결하고 실패 후 다른 경로를 사용합니다. `via`로 사용 경로를 확인하세요.
- **프록시 추가 직후 모두 `connect`에서 실패** — GATT 연결용 `bluetooth_proxy.active: true`와 여유 슬롯을 확인하세요. `esp32_ble_tracker.scan_parameters.active`는 별도로 스캔 응답 요청을 제어합니다. 수동 스캔 자체가 GATT 연결을 막지는 않습니다. [공통 Bluetooth 문제 해결](https://github.com/eigger/blesession/blob/main/docs/troubleshooting.ko.md)을 참고하세요.
- **성공한 폴링에서 `connect_attempts`가 1보다 큼** — 연결 직후 안정화 중 끊겼다가 재시도로 성공했습니다. 가끔이면 문제가 없지만 대부분의 폴링에서 발생하면 약한 연결이나 본딩이 없는 프록시를 의심하세요.
- **매번 `time_sync_error`** — 기록은 도착하지만 시각을 설정하지 못합니다. EEPROM으로 시계를 동기화하는 모델은 시계 레이아웃이 잘못됐을 수 있으므로 모델명과 함께 제보하세요.

### 속성에 드러나지 않는 경우

다음 세 경우는 세션이 실패하지 않았거나 시작하지 않았기 때문에 실패 센서에 나타나지 않습니다.

**폴링은 성공했지만 측정값이 나타나지 않음.** `success: true`이고 `records`가 1 이상이면 기기의 최신 기록이 이미 표시된 기록일 수 있습니다. 새 측정값이 이 항목에서 읽지 않는 사용자 슬롯에 들어갔는지 기기의 사용자 스위치를 확인하세요. `records: 0`이면 읽는 사용자에 저장된 기록이 없습니다. 둘 다 실패가 아니므로 Last Failure가 바뀌지 않습니다.

**세션 자체를 시도하지 않음.** 폴링 시점에 어떤 무선 장치도 기기를 보지 못했다면 연결하지 않고 Duration도 바뀌지 않습니다. 폴링은 캐시 값을 반환하며 기본 로그 수준에서는 기록하지 않습니다. 측정 후 광고하는 동안 Bluetooth 기호를 확인하고 *Refresh Data*를 누르세요.

**BLE 통신 전에 액션 자체가 실패함.** 범위 밖에서 *Retry Pairing*을 누르면 *BLE device not available*로 즉시 실패합니다. *BLE session already in progress*는 폴링이 진행 중이라는 뜻이므로 Connection이 꺼진 뒤 재시도하세요.

### 간헐적인 실패

간헐적인 실패를 살펴볼 수 있도록 Last Failure는 속성을 유지합니다. 이후 성공한 세션을 표시하는 Duration 대신 Last Failure를 확인하세요. 실패 당시의 `failed_stage`와 `rssi`가 중요합니다. 특정 `via`에 실패가 몰리면 해당 프록시를 조사하세요. 매번 `paths`가 1이었다면 다른 무선 장치가 대체 경로를 제공할 수 있습니다.

## 진단 및 로그

### 확인 위치

기기의 페이지(**설정 → 기기 및 서비스 → Omron → 기기**)를 열고 *진단*에서 다음 다섯 엔티티를 순서대로 확인하세요. 아래 영어 이름은 UI 언어에 따라 번역되어 표시될 수 있습니다.

| 엔티티 | 의미 |
|---|---|
| **Last Failure** | 마지막으로 BLE 세션이 **실패한 시각**입니다. 속성에는 해당 세션의 `failed_stage`, `likely_cause`, `error`, 무선 정보(`via`, `rssi`, `paths`)와 단계별 시간이 있습니다. 다음 실패까지 유지하므로 아침 폴링이 성공해도 전날 밤 실패를 볼 수 있습니다. |
| **Duration** | 성공 여부와 관계없이 **가장 최근** BLE 세션의 소요 초입니다. 종료할 때 세션 세부 속성이 붙고 진행 중(Connection 켜짐)에는 없습니다. 조사할 세션이 가장 최근이면 사용하세요. |
| **Failure Count** | 통합을 (다시) 로드한 뒤 실패한 세션 수입니다. `connect` 실패도 포함되며 기기가 잠들면 여기서 실패하므로 매일 조금씩 증가할 수 있습니다. 매번 증가하거나 측정값도 도착하지 않을 때 조사하세요. 숫자를 해석하기 전에 Last Failure의 `failed_stage`를 확인하세요. |
| **Last Readout** | 마지막으로 폴링이 **기록을 디코딩한 시각**입니다. 연결됐지만 기록이 없으면 바뀌지 않습니다. “Duration만 갱신”되면 접근은 가능하지만 기록이 없거나 읽기 전 실패한 경우입니다. |
| **Connection** (이진) | **세션 진행 중** 켜집니다. 기기가 가까이 있다는 뜻은 아닙니다. 기기는 측정 사이에 무선을 끕니다. |

속성은 엔티티 → ⋮ → **속성**에서 보거나 **개발자 도구 → 상태**에서 엔티티를 검색하세요. 템플릿 예:

```jinja
{{ state_attr('sensor.hem_7155t_a1b2_last_failure', 'likely_cause') }}
```

ID는 모델명과 기기 MAC의 마지막 네 자리 16진수로 구성됩니다.

Home Assistant는 Duration의 상태 변경 때 속성도 기록하므로 이 센서의 기록에서 마지막 실패뿐 아니라 각 세션의 세부 정보를 확인할 수 있습니다. 재시작·다시 로드하면 두 센서는 비어 있는 상태로 시작합니다. 그전 실패는 기록에서 찾아보세요.

### 속성 읽기

속성은 다음 순서입니다. `failed_stage`는 BLE 통합의 공통 단계명이며 `failed_detail`은 기기 고유 단계명으로 두 이름이 다를 때만 표시됩니다.

| 속성 | 의미 |
|---|---|
| `operation` | `poll`(예약 또는 수동 기록 읽기), `pairing`(*Retry Pairing* 버튼 또는 자동 페어링 광고), `time_sync`. |
| `success` | 세션 완료 여부. |
| `error`, `failed_stage`, `failed_detail`, `likely_cause` | 실패 때만 표시하는 정확한 오류, 실패한 공통 단계, 기기 단계명과 일반적인 원인 설명. |
| `likely_cause_key` | 공통 원인 설명(`connect.no_slot`, `link_lost`, `unreachable` 등)일 때만 있는 고정 이름. 자동화에서 영어 문장 대신 비교할 수 있습니다. 기기 고유 설명에는 없습니다. |
| `via`, `via_type`, `rssi`, `paths` | 연결 경로(프록시/로컬 어댑터), 해당 무선 장치가 마지막으로 받은 기기 신호와 현재 기기를 볼 수 있는 연결 가능 무선 장치 수. `paths: 1`이면 대체 무선 경로가 없습니다. `connect` 실패 때 `via`는 시도한 경로입니다. |
| `via_unconfirmed` | `true`일 때만 표시됩니다. `via`가 확인된 연결 경로가 아니라 가장 강하게 광고를 수신한 무선 장치라는 뜻입니다. 연결 실패 때 흔한 추정값입니다. |
| `advertised_via` | `via`와 다를 때만 표시하는 가장 강한 광고의 수신 경로로, Home Assistant가 먼저 시도하는 경로입니다. 실제 링크가 다른 곳이면 대체 연결이거나 여러 프록시 중 본딩을 보유한 프록시일 수 있습니다. |
| `connect_s`, `services_s`, `pair_s`, `unlock_s`, `memory_open_s`, `time_sync_s`, `readout_s`, `device_info_s`, `registration_s`, `memory_close_s`, `disconnect_s` | 실행 순서에 따른 각 단계의 소요 초. 실행하지 않은 단계는 없으며 기기 고유 명칭을 유지합니다. |
| `connect_attempts`, `bonded_at_connect` | 연결 시도 횟수(최대 3회, 연결 직후 안정화 중 끊기면 재시도)와 연결 중 본딩 여부(로컬 어댑터의 페어링 세션). |
| `adopted_link` | 폴링이 직접 연결하는 대신 직전 페어링 세션에서 연 연결을 사용했다는 뜻. |
| `memory_session_attempts` | 잠금 해제와 기록 읽기 세션 열기 시도 횟수(최대 3회). |
| `records` | 읽어 온 최신 기록 수로 데이터가 있는 사용자마다 하나. `0`이면 저장 기록이 없어 측정 엔티티가 바뀌지 않은 것이며 실패가 아닙니다. |
| `time_sync_error`, `registration_error` | 시각 쓰기 또는 페어링 등록이 실패했지만 세션은 계속 진행된 경우. |

### 디버그 로그

위 진단 엔티티를 제공하며 별도 다운로드용 통합 진단 파일은 제공하지 않습니다. **설정 → 기기 및 서비스 → Omron → 디버그 로깅 활성화**로 켜고 한 번 재현한 뒤 디버그 로깅을 끄면 해당 구간 로그를 다운로드할 수 있습니다. 또는 기존 `configuration.yaml`의 logger 설정에 다음을 합친 뒤 Home Assistant를 재시작하세요.

```yaml
logger:
  default: warning
  logs:
    custom_components.omron: debug
```

수집 후 `custom_components.omron: debug` 설정을 제거하고 재시작해 디버그 로그를 끄세요. 이 로거에는 통합의 `omron_ble` 모듈도 포함됩니다. 재현 구간만 수집하세요. 로그·속성·트레이스를 공개하기 전에 기기 주소, 본딩 키/전송 자격 증명, 토큰과 개인 측정값을 제거하세요. Bluetooth 스택 캡처에는 보안 정보와 건강 데이터가 있으므로 민감 정보를 제거한 요약 방법을 포함한 [Bluetooth 로그 수집](capturing-bluetooth-logs.md)(영어)을 따르세요. iPhone sysdiagnose 압축 파일 자체는 업로드하지 마세요.

## 문제가 계속되는 경우

이 통합의 [기존 Issues](https://github.com/eigger/hass-omron/issues)를 이용하고 [Bluetooth 로그 수집](capturing-bluetooth-logs.md)(영어)의 기존 제보 안내를 따라주세요.

### 이슈에 첨부할 자료

1. **Last Failure** 속성(개발자 도구 → 상태 → 엔티티 → 속성 복사). 실패가 최근 세션이 아니면 그 시각의 **Duration** 기록 속성도 첨부하세요.
2. 항목에서 선택한 모델과 기기 라벨에 인쇄된 모델.
3. `connect` 또는 `transfer` 실패라면 사용 무선 경로(`via`), 프록시 모델과 ESPHome 버전 또는 어댑터 정보.
4. 페어링은 되지만 읽지 못하거나 값이 틀리면 휴대폰 앱의 Bluetooth 로그. [로그 수집](capturing-bluetooth-logs.md)(영어)에 방법과 포함 정보가 있습니다.
5. Home Assistant와 통합 버전, 확인 가능한 경우 설치된 blesession 버전, 기기/프록시 모델과 리비전, ESPHome 버전 또는 로컬 어댑터 정보, 연결 방식, 실패 시각/단계와 재현 순서. 성공한 시도와 실패한 시도를 구분하세요.
