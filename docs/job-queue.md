# PHASE 7 - Job Manager and Session Lock

## 1. 목적

PHASE 7은 Discord에서 연속으로 들어오는 Codex 작업 요청을 SQLite job queue에 영구 저장하고, 같은 Codex session에 동시에 둘 이상의 외부 queue dispatch가 발생하지 않도록 직렬화한다.

```text
Discord message
   |
   v
Discord allowlist
   |
   v
JobManager
   |
   +--> jobs INSERT (discord_message_id UNIQUE)
   |
   +--> per-session asyncio.Lock
   |
   +--> session busy? ---- yes ---> QUEUED
   |
   no
   |
   +--> DISPATCHED durable commit
   |
   +--> codex queue --thread ... --message ...
   |
   +--> completion notify
          |
          +--> COMPLETED
```

PHASE 7에서는 queued job의 자동 실행을 의도적으로 하지 않는다. 자동 실행은 PHASE 9의 `auto_run` 정책에서 추가한다.

---

## 2. SQLite schema v3

PHASE 7에서 schema version은 `3`으로 올라갔다. 현재 전체 프로젝트 schema는 PHASE 9의 `sessions.auto_run` migration까지 반영되어 `4`이며, v1/v2/v3 데이터베이스는 자동으로 최신 schema까지 migration한다.

`jobs`:

```text
id                    INTEGER PRIMARY KEY
discord_message_id    INTEGER UNIQUE
thread_id             TEXT
prompt                TEXT
status                QUEUED | DISPATCHED | COMPLETED | FAILED | CANCELLED
created_at
started_at
completed_at
exit_code
error
```

핵심 DB 제약:

```text
UNIQUE(discord_message_id)
```

Discord Gateway reconnect 또는 event replay로 같은 message를 다시 받아도 동일 job이 새로 만들어지지 않는다.

또한 partial unique index를 사용한다.

```text
UNIQUE(thread_id) WHERE status = 'DISPATCHED'
```

따라서 application lock에 문제가 생겨도 같은 Codex session에는 최대 하나의 `DISPATCHED` job만 존재할 수 있다.

---

## 3. Prompt 저장 정책

PHASE 7 이후 queued job을 서비스 재시작 후에도 유지해야 하므로 job의 prompt 본문은 SQLite에 저장한다.

운영 DB는 반드시 service user만 접근할 수 있어야 하며 현재 Database 계층은 DB 파일 mode를 `0600`으로 설정한다.

로그에는 prompt 본문을 기록하지 않는다. `JobManager` 로그는 job ID, thread ID, prompt 길이 등 메타데이터만 기록한다.

---

## 4. Job 상태 전이

정상 경로:

```text
QUEUED
  |
  v
DISPATCHED
  |
  v
COMPLETED
```

명확한 Codex queue 실패:

```text
QUEUED
  |
  v
DISPATCHED
  |
  v
FAILED
```

사용자 취소:

```text
QUEUED -> CANCELLED
```

LIVE mode의 `DISPATCHED` job은 `/cancel`로 취소하지 않는다. 이미 외부 Codex queue side effect가 발생했을 수 있기 때문이다. PHASE 8부터 MANAGED mode의 active `DISPATCHED` job은 Codex Dispatch가 child process를 직접 소유하므로 `/cancel`로 종료할 수 있다.

---

## 5. 외부 side effect와 crash 정책

Codex queue를 호출하기 **전에** job을 `DISPATCHED`로 durable commit한다.

```text
DB: DISPATCHED commit
        |
        v
codex queue
```

이 순서는 process가 외부 명령 성공 직후 DB update 전에 죽었을 때 같은 prompt를 다시 보내는 위험을 줄인다.

서비스가 `DISPATCHED` 상태를 남긴 채 재시작하면 Codex Dispatch는 해당 job을 자동으로 재전송하지 않는다. 대신 session을 `RUNNING`으로 복구하고 기존 completion notify를 기다린다.

즉 PHASE 7은 ambiguous crash 상황에서 **중복 실행 방지**를 자동 재시도보다 우선한다.

---

## 6. Session Lock

JobManager는 Codex `thread-id`별 `asyncio.Lock`을 유지한다.

같은 Discord Thread에 메시지 세 개가 동시에 들어와도:

```text
Job #1 -> DISPATCHED
Job #2 -> QUEUED
Job #3 -> QUEUED
```

형태가 된다.

SQLite partial unique index가 두 번째 방어선이다.

---

## 7. Completion 처리

Codex `agent-turn-complete` 이벤트가 들어오면:

1. SessionManager가 session을 `IDLE`로 갱신한다.
2. JobManager가 해당 session의 단일 `DISPATCHED` job을 `COMPLETED`로 전환한다.
3. CompletionNotifier가 결과를 Discord에 전송한다.
4. `QUEUED` job은 그대로 유지한다.

PHASE 7에서는 4번 이후 자동 dispatch하지 않는다.

---

## 8. Discord application commands

PHASE 7은 허용 Guild에 다음 application command를 등록한다.

### `/status`

현재 Discord Thread와 연결된 Codex session 상태를 보여준다.

예:

```text
Session: abcdef12
Mode: LIVE
Status: RUNNING
Active: Job #12
Queued: 2
```

### `/queue`

현재 `QUEUED` 및 `DISPATCHED` job 최대 10개를 표시한다.

### `/cancel job_id:<ID>`

현재 Thread의 `QUEUED` job을 취소한다. PHASE 8부터는 MANAGED mode의 active `DISPATCHED` job도 process terminate/kill 후 `CANCELLED`로 전환할 수 있다. LIVE `DISPATCHED` job은 취소하지 않는다.

명령 결과는 ephemeral response로 반환한다.

Guild/Channel/User allowlist가 application command에도 동일하게 적용된다.

---

## 9. Restart recovery

서비스 시작 시 `JobManager.recover()`를 실행한다.

정책:

- `QUEUED`: 그대로 유지
- LIVE `DISPATCHED`: 재전송하지 않고 session `RUNNING` 복구
- MANAGED `DISPATCHED`: child process가 orchestrator와 함께 사라졌으므로 `FAILED` + session `ERROR`
- `DISPATCHED`가 존재하는 session: `RUNNING`으로 복구
- `COMPLETED`, `FAILED`, `CANCELLED`: 변경하지 않음

이후 실제 Codex completion notify가 도착하면 `DISPATCHED -> COMPLETED`가 정상 처리된다.

---

## 10. Host Acceptance

실제 Codex live session이 매핑된 Discord Thread에서 첫 번째 prompt가 아직 작업 중일 때 후속 prompt 두 개를 빠르게 전송한다.

그 후:

```bash
cd /home/your-user/codex-dispatch
set -a
source /etc/codex-dispatch/codex-dispatch.env
set +a
PYTHONPATH=src python3 scripts/job-host-acceptance.py \
  --minimum-jobs 3 \
  --minimum-queued 1
```

예상 예:

```text
schema_version=4
jobs_total=3
jobs_queued=2
jobs_dispatched=1
jobs_completed=0
jobs_failed=0
jobs_cancelled=0
Codex Dispatch PHASE 7 acceptance: PASS
```

첫 job이 이미 끝난 경우 `jobs_completed=1`, `jobs_dispatched=0`일 수 있지만, 후속 job이 자동 실행되지 않았기 때문에 최소 하나 이상의 `QUEUED` job이 남아 있어야 한다.

Discord에서도 다음을 확인한다.

```text
/status
/queue
/cancel job_id:<queued-job-id>
```

---

## 11. 완료 기준

다음을 모두 만족하면 PHASE 7 PASS다.

- schema v3 jobs migration 및 현재 schema v4 호환 성공
- job prompt/status persistent 저장
- Discord message ID 영구 idempotency
- 같은 session에 `DISPATCHED <= 1`
- 동시 입력 3개가 중복 dispatch되지 않음
- explicit queue failure가 `FAILED`로 남음
- completion notify가 active job을 `COMPLETED`로 닫음
- queued job cancel 가능
- `/status`, `/queue`, `/cancel` authorization 적용
- restart 시 `DISPATCHED` 자동 재전송 없음
- 기존 PHASE 0~6 regression 없음
