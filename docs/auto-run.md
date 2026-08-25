# PHASE 9 - Queue Auto Run

## 1. 목적

PHASE 9는 PHASE 7의 persistent `jobs` queue를 사용해 사용자가 명시적으로 허용한 session에서 후속 Job을 하나씩 자동 실행한다.

기본값은 항상 OFF다.

```text
auto_run = OFF
```

일반 흐름:

```text
Job #1 DISPATCHED
Job #2 QUEUED
Job #3 QUEUED
        |
        | /auto state:on
        v
Job #1 completion notify
        |
        v
Job #1 COMPLETED
        |
        v
Job #2 DISPATCHED
        |
        v
Job #2 completion notify
        |
        v
Job #2 COMPLETED
        |
        v
Job #3 DISPATCHED
```

한 session에 동시에 둘 이상의 `DISPATCHED` job은 존재할 수 없다.

---

## 2. SQLite schema v4

PHASE 9에서 schema version은 `4`로 올라간다.

`sessions`에 다음 필드가 추가된다.

```text
auto_run INTEGER NOT NULL DEFAULT 0
```

의미:

- `0`: OFF
- `1`: ON

기존 v1/v2/v3 DB는 자동으로 v4까지 migration되고 모든 기존 session은 안전하게 `auto_run=0`으로 시작한다.

---

## 3. Discord command

### `/auto state:on`

현재 session에 auto-run을 활성화한다.

현재 상태가 `RUNNING` 또는 `WAITING`이면 active Job은 그대로 진행하고 completion 이후 다음 `QUEUED` Job을 실행한다.

session이 `IDLE`이고 이미 `QUEUED` Job이 있다면 첫 Job을 즉시 durable claim한 뒤 실행한다.

`ERROR` 또는 `OFFLINE` session에서는 auto-run을 활성화하지 않는다.

### `/auto state:off`

새 후속 Job의 자동 실행만 중단한다.

이미 `DISPATCHED`된 Job은 취소하지 않는다.

`/status`에는 다음 항목이 표시된다.

```text
Auto Run: ON
```

또는:

```text
Auto Run: OFF
```

---

## 4. Completion 처리

실제 Codex `agent-turn-complete` notify가 들어오면 순서는 다음과 같다.

1. SessionManager가 session을 `IDLE`로 갱신한다.
2. JobManager가 현재 `DISPATCHED` Job을 `COMPLETED`로 전환한다.
3. `auto_run=1`인지 확인한다.
4. 가장 오래된 `QUEUED` Job 하나를 선택한다.
5. DB에서 `DISPATCHED`로 먼저 commit한다.
6. background dispatch task를 시작한다.
7. LIVE는 `codex queue`, MANAGED는 `codex exec resume`으로 실행한다.

자동 dispatch는 completion notify handler가 Managed subprocess 전체 종료를 기다리지 않도록 background task로 분리한다.

---

## 5. 실패 시 pause

자동 실행 중 다음과 같은 실패가 발생하면 해당 Job은 `FAILED`가 되고 session의 `auto_run`은 즉시 `0`으로 내려간다.

예:

- Codex live session unavailable
- queue timeout
- Codex queue rejection
- Managed resume failure
- Managed timeout
- Managed session unavailable
- service shutdown 중 Managed execution 중단
- Managed orphan job restart recovery

즉:

```text
Job #2 FAILED
    |
    v
auto_run = OFF
    |
    v
Job #3 remains QUEUED
```

실패 뒤 다음 queued Job을 자동으로 계속 실행하지 않는다.

사용자가 원인을 확인하고 다시:

```text
/auto state:on
```

을 명시적으로 실행해야 한다.

---

## 6. Cancel 정책

`QUEUED` Job을 `/cancel`하면 해당 Job만 `CANCELLED`가 된다.

Auto Run이 ON이면 다음 completion 시 CANCELLED Job은 자동으로 건너뛰고 가장 오래된 실제 `QUEUED` Job을 선택한다.

Active MANAGED Job을 `/cancel`하면 process를 종료하고 Job을 `CANCELLED`로 전환하며 Auto Run도 OFF로 내린다. 사용자가 실행 중인 작업을 직접 중단한 직후 다른 queued Job이 자동 시작되는 것을 방지하기 위한 정책이다.

LIVE `DISPATCHED` Job은 PHASE 9에서도 원격 cancel하지 않는다.

---

## 7. Restart recovery

`auto_run`은 SQLite에 영구 저장된다.

서비스 restart 시 먼저 기존 PHASE 7/8 recovery를 적용한다.

### LIVE DISPATCHED

외부 TUI가 실제로 계속 실행 중일 가능성이 있으므로:

```text
DISPATCHED 유지
session RUNNING
재전송하지 않음
```

Auto Run이 ON이더라도 해당 completion notify가 올 때까지 다음 Job을 실행하지 않는다.

### MANAGED DISPATCHED

orchestrator와 child process가 함께 사라졌으므로:

```text
Job FAILED
session ERROR
auto_run OFF
```

으로 복구한다.

### IDLE + auto_run ON + QUEUED

Discord Gateway가 ready 상태가 된 뒤 안전한 session만 자동 재개한다.

조건:

```text
auto_run = ON
status = IDLE
DISPATCHED job 없음
QUEUED job 있음
workspace enabled
Discord Thread mapping 존재
```

reconnect 때문에 `on_ready`가 여러 번 호출되어도 DB claim과 session lock 때문에 같은 Job을 두 번 dispatch하지 않는다.

---

## 8. LIVE / MANAGED 공통 serialization

두 mode 모두 동일한 persistent job queue를 사용한다.

```text
one Codex thread
    -> at most one DISPATCHED job
```

을 SQLite partial unique index와 `asyncio.Lock` 양쪽에서 강제한다.

Auto Run은 mode를 변경하지 않는다.

---

## 9. Host Acceptance

실제 Discord session Thread에서 다음 순서로 검증한다.

1. `/auto state:on`
2. 첫 Job을 실행한다.
3. 작업 중 후속 prompt 두 개를 보낸다.
4. `/queue`에서 후속 Job이 `QUEUED`인지 확인한다.
5. 각 completion마다 다음 Job이 정확히 하나씩 실행되는지 확인한다.
6. 최종적으로 3개 이상이 `COMPLETED`인지 확인한다.

DB 검증:

```bash
cd /home/your-user/codex-dispatch

set -a
source /etc/codex-dispatch/codex-dispatch.env
set +a

PYTHONPATH=src python3 scripts/auto-run-host-acceptance.py \
  --thread <CODEX_THREAD_ID> \
  --minimum-completed 3
```

PASS 예:

```text
schema_version=4
thread_id=...
mode=LIVE
auto_run=on
jobs_completed=3
jobs_queued=0
jobs_dispatched=0
Codex Dispatch PHASE 9 acceptance: PASS
```

실패 pause도 별도로 확인한다.

1. `/auto state:on`
2. 테스트 가능한 실패를 발생시킨다.
3. `/status`에서 `Auto Run: OFF` 확인
4. 후속 `QUEUED` Job이 자동 실행되지 않는지 확인

---

## 10. 완료 기준

PHASE 9 완전 PASS 조건:

- schema v4 migration 성공
- 기존 session의 auto-run default OFF
- `/auto state:on|off` 동작
- LIVE queue 순차 자동 실행
- MANAGED queue 순차 자동 실행
- 동시에 하나의 `DISPATCHED` job만 존재
- CANCELLED queued Job skip
- active Managed cancel 시 auto-run OFF
- dispatch/process failure 시 auto-run OFF
- restart 후 safe idle queue만 재개
- LIVE ambiguous active Job 재전송 없음
- MANAGED orphan Job failure + auto-run pause
