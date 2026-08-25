# Codex CLI Capability Discovery

## 1. 목적

이 문서는 `Codex Dispatch` PHASE 0의 실행 결과와 검증 방법을 기록한다.

PHASE 0의 목적은 Codex CLI 명령 형식을 추측해서 애플리케이션에 하드코딩하지 않고, 실제 VPS에서 Codex를 실행하는 Linux 사용자 환경을 기준으로 다음 기능을 먼저 확인하는 것이다.

- 설치된 Codex CLI 버전
- `codex queue` 지원 여부
- `codex queue --thread --message` 인터페이스
- `codex exec` 지원 여부
- `codex exec resume` 지원 여부
- external `notify` 동작 여부
- notify payload의 `thread-id`, `turn-id`, `cwd`, `client` 존재 여부
- live Codex session에 외부 프로세스가 queue message를 전달할 수 있는지
- managed/headless resume의 실제 동작

---

## 2. PHASE 0 구현 산출물

다음 도구가 추가되었다.

### `scripts/codex-capability-discovery.sh`

실제 Codex 사용자의 환경에서 실행하는 **수동적(passive) capability probe**다.

이 스크립트는 다음을 확인한다.

- `codex` executable 탐색
- `codex --version`
- `codex queue --help`
- queue의 `--thread`, `--message` option
- `codex exec --help`
- `codex exec resume --help`
- `codex doctor --help`
- `~/.codex/config.toml`의 readable 여부
- top-level `notify` 설정 존재 여부

중요: 이 스크립트는 Codex turn을 시작하지 않고, 기존 session에 메시지를 보내지 않으며, Codex 설정을 수정하지 않는다.

실행:

```bash
cd /home/ivenewjeans25/codex-dispatch
bash scripts/codex-capability-discovery.sh
```

Codex가 PATH에 없다면:

```bash
bash scripts/codex-capability-discovery.sh \
  --codex /absolute/path/to/codex
```

---

### `bin/codex-notify-probe.py`

Codex external notify payload의 실제 schema를 검증하기 위한 임시 probe다.

이 probe는 privacy를 위해 다음 실제 값을 저장하지 않는다.

- 실제 prompt text
- 마지막 assistant message text
- 전체 `cwd`
- 실제 thread ID
- 실제 turn ID

대신 다음만 JSONL로 기록한다.

- payload key 목록
- event type
- thread ID 존재 여부
- turn ID 존재 여부
- cwd 존재 여부
- client 존재 여부
- input message 개수
- assistant message 존재 여부

기본 출력:

```text
/tmp/codex-dispatch-notify-probe.jsonl
```

파일 권한은 `0600`으로 강제한다.

---

### `scripts/codex-live-session-probe.sh`

`codex queue`를 이용해 기존 live Codex session에 실제 메시지를 보내는 **능동적(active) probe**다.

이 도구는 실제 Codex turn을 발생시킬 수 있기 때문에 passive discovery와 분리되어 있으며 자동으로 실행하지 않는다.

실행 형식:

```bash
bash scripts/codex-live-session-probe.sh \
  --thread <CODEX_THREAD_ID>
```

기본 probe prompt는 파일을 수정하지 않고 정확히 다음 문자열만 응답하도록 요구한다.

```text
CODEX_DISPATCH_QUEUE_OK
```

CLI exit code 0만으로 PASS 처리하지 않는다. 실제 target TUI/session이 메시지를 수신하고 위 응답을 생성해야 live delivery PASS다.

---

### `tests/fixtures/codex-notify-agent-turn-complete.json`

통합 테스트를 위한 비식별 fixture다.

현재 upstream legacy notify schema의 주요 필드를 반영하되 실제 사용자 ID, path, prompt, 결과를 포함하지 않는다.

---

## 3. Upstream 기준선 - 2026-08-24

PHASE 0 구현 시 공개된 OpenAI Codex upstream도 함께 확인했다.

### 최신 stable 기준

2026-08-20 공개된 Codex CLI `0.149.0`에는 기존 local/remote session으로 메시지를 보내는 `codex queue`가 추가되었다.

OpenAI Codex 현재 `main`의 top-level CLI에도 `Queue a message for an existing session` 용도의 `Queue` subcommand가 존재한다.

따라서 Codex Dispatch는 `0.149.0+`에서 `codex queue`를 우선적인 live-session 전달 방식으로 사용할 수 있을 것으로 예상한다. 단, **실제 VPS 설치 버전과 help output이 최종 계약(contract)** 이다.

### Thread ID

현재 upstream thread store는 `codex_protocol::ThreadId`를 durable thread handle로 취급한다. 또한 app-server protocol의 thread data 문서에는 Codex가 생성하는 thread ID가 UUIDv7임이 명시되어 있다.

따라서 Codex Dispatch의 session primary key 후보는 `thread-id`가 적합하다.

### Legacy external notify

현재 upstream `legacy_notify` 구현은 `AfterAgent` 이벤트를 다음 형태의 `agent-turn-complete` notification으로 변환한다.

필드:

```text
type
thread-id
turn-id
cwd
client
input-messages
last-assistant-message
```

현재 external top-level `notify`는 completion 알림 중심으로 사용해야 하며 approval/request-user-input 같은 이벤트까지 동일 경로로 온다고 가정하지 않는다.

---

## 4. MCP 구현 환경에서 확인된 사항

2026-08-24 `creator-alliance` MCP workspace에서 다음 환경 차이가 확인되었다.

```text
workspace: /home/ivenewjeans25/codex-dispatch
MCP shell user: devspace
project owner: ivenewjeans25
```

MCP shell의 PATH:

```text
/var/lib/devspace/.pi/agent/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

이 환경에서는:

```text
codex: command not found
```

이며 `devspace` 사용자는 `/home/ivenewjeans25` 자체를 열람할 권한도 없다.

따라서 MCP shell에서 나온 `codex not found` 결과는 **실제 VPS의 Codex 설치 실패를 의미하지 않는다.** Codex CLI가 로그인되고 실제 TUI가 실행되는 `ivenewjeans25` 사용자 shell을 기준으로 acceptance probe를 실행해야 한다.

MCP 작업환경이 다른 사용자라는 점 때문에 PHASE 0에서 user/PATH detection을 명시적인 capability 항목으로 구현했다.

---

## 5. Passive Acceptance Probe

실제 Codex 사용자 shell에서 실행한다.

```bash
cd /home/ivenewjeans25/codex-dispatch
bash scripts/codex-capability-discovery.sh
```

최소 기대 조건:

```text
codex_binary_found: yes
codex queue: yes
queue --thread option: yes
queue --message option: yes
codex exec: yes
codex exec resume: yes
```

`codex queue`가 없다면 installed version과 `codex --help`를 기준으로 업그레이드 또는 alternate integration을 결정한다. Codex Dispatch core에서 존재하지 않는 명령을 호출하는 fallback은 만들지 않는다.

---

## 6. Notify Payload Acceptance Probe

### 6.1 임시 설정

기존 `~/.codex/config.toml`의 notify 설정이 있다면 먼저 보존해야 한다. PHASE 0 확인 중에만 다음과 같이 probe를 연결한다.

```toml
notify = [
  "python3",
  "/home/ivenewjeans25/codex-dispatch/bin/codex-notify-probe.py"
]
```

기존 notify를 무단으로 덮어쓰지 않는다.

### 6.2 Codex turn 실행

일반 Codex TUI에서 파일 변경이 필요 없는 간단한 turn 하나를 완료한다.

완료 후:

```bash
cat /tmp/codex-dispatch-notify-probe.jsonl
```

실제 prompt와 assistant text는 이 파일에 기록되지 않아야 한다.

### 6.3 Discovery report와 결합

```bash
bash scripts/codex-capability-discovery.sh \
  --notify-probe /tmp/codex-dispatch-notify-probe.jsonl
```

PASS 조건:

```text
last_event_type: agent-turn-complete
thread_id_present: true
turn_id_present: true
cwd_present: true
```

`client`는 사용 surface/version에 따라 optional일 수 있으므로 core session identity에는 사용하지 않는다.

---

## 7. Live Queue Acceptance Probe

### 사전조건

- Codex TUI session 하나가 실행 중이어야 한다.
- target thread ID를 알고 있어야 한다.
- passive probe에서 `codex queue`, `--thread`, `--message`가 확인되어야 한다.

실행:

```bash
bash scripts/codex-live-session-probe.sh \
  --thread <THREAD_ID>
```

PASS 조건:

1. queue CLI가 성공한다.
2. target Codex session이 메시지를 받는다.
3. 같은 thread/context에서 새 turn이 실행된다.
4. Codex가 `CODEX_DISPATCH_QUEUE_OK`라고 응답한다.
5. 파일이 수정되지 않는다.

이 결과가 확인되면 Discord -> Codex live-session 경로는 `codex queue`를 기반으로 구현한다.

---

## 8. Concurrent Queue 동작 확인

이 검증은 두 개의 실제 Codex turn을 만들 수 있으므로 자동화 스크립트에서 실행하지 않는다.

동일 thread에 짧은 no-op prompt 두 개를 거의 동시에 enqueue하고 다음을 확인한다.

- 두 command가 session corruption을 일으키지 않는가
- 처리 순서가 FIFO인가
- active turn 중 enqueue한 prompt가 현재 turn을 interrupt/steer하지 않고 다음 turn으로 처리되는가
- 외부 queue writer가 idle session을 깨워 처리하게 하는가

Codex Dispatch 자체에서는 upstream 동작과 별개로 session별 dispatch lock을 사용하므로, MVP는 같은 session에 여러 subprocess를 동시에 실행하지 않는다.

---

## 9. Stale/Closed Session 동작 확인

존재하지 않거나 더 이상 queue delivery가 불가능한 thread ID를 대상으로 `codex queue`를 실행하여 다음을 기록한다.

- exit code
- stderr 형태
- retry 가능한 오류인지
- session을 `OFFLINE`으로 전환해야 하는 오류인지

실제 구현에서는 stderr 문자열 하나에 의존하지 않고 exit code + capability context를 함께 사용한다.

---

## 10. Managed Resume Acceptance Probe

`codex exec resume`은 실제 model turn을 발생시키므로 passive discovery에서는 help interface만 확인한다.

별도 test session에서 다음을 검증한다.

1. 기존 session을 `codex exec resume`으로 재개한다.
2. 이전 context에만 존재하는 정보를 물어 context 보존 여부를 확인한다.
3. exit code와 stdout/stderr behavior를 기록한다.
4. 같은 thread가 live TUI에서 active한 동안 별도 resume process를 동시에 실행하지 않는다.

PASS하면 PHASE 8 Managed/Headless Mode의 adapter 계약으로 사용한다.

---

## 11. Capability Matrix

| Capability | Upstream 2026-08-24 | VPS Host Acceptance |
| --- | --- | --- |
| Durable `thread-id` | YES | PENDING USER-SHELL PROBE |
| external `agent-turn-complete` notify | YES | PENDING NOTIFY PROBE |
| notify includes `thread-id` | YES | PENDING NOTIFY PROBE |
| notify includes `turn-id` | YES | PENDING NOTIFY PROBE |
| notify includes `cwd` | YES | PENDING NOTIFY PROBE |
| `codex queue` | YES in 0.149.0/current main | PENDING PASSIVE PROBE |
| queue `--thread` / `--message` | expected current interface | PENDING PASSIVE PROBE |
| live TUI external queue delivery | supported upstream concept | PENDING ACTIVE PROBE |
| `codex exec resume` | expected supported | PENDING PASSIVE + ACTIVE PROBE |
| same-session concurrency behavior | do not assume | PENDING CONTROLLED PROBE |
| stale thread error behavior | do not assume | PENDING CONTROLLED PROBE |

---

## 12. PHASE 0 완료 판정

### 구현 상태

- [x] passive capability discovery tool 구현
- [x] privacy-preserving notify probe 구현
- [x] active live-session queue probe 구현
- [x] sanitized notify fixture 추가
- [x] upstream baseline 조사
- [x] MCP user/PATH 차이 식별
- [x] Codex command syntax를 capability detection 뒤에 두도록 설계

### Host acceptance 상태

- [ ] 실제 Codex 사용자로 `codex --version` 확인
- [ ] 실제 Codex 사용자로 `codex queue --help` 확인
- [ ] 실제 Codex 사용자로 `codex exec resume --help` 확인
- [ ] 실제 notify event 캡처
- [ ] notify에 `thread-id`, `turn-id`, `cwd` 존재 확인
- [ ] live TUI에 외부 queue message 전달 확인
- [ ] same-session controlled concurrency 확인
- [ ] stale/closed session error behavior 확인
- [ ] managed resume context preservation 확인

위 Host acceptance가 모두 완료되기 전까지 PHASE 1 이후 코드에서 Codex CLI의 세부 실행 계약을 확정된 것으로 간주하지 않는다.

---

## 13. Upstream 참고 자료

- Codex CLI 0.149.0 release: `https://github.com/openai/codex/releases/tag/rust-v0.149.0`
- Current CLI command definitions: `https://github.com/openai/codex/blob/main/codex-rs/cli/src/main.rs`
- Legacy external notify payload: `https://github.com/openai/codex/blob/main/codex-rs/hooks/src/legacy_notify.rs`
- Durable thread-store interface: `https://github.com/openai/codex/blob/main/codex-rs/thread-store/src/lib.rs`
- Thread data protocol: `https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs`
