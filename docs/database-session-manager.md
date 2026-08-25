# PHASE 4 - Database and Session Manager

## 1. 목적

PHASE 4는 Codex completion notify를 SQLite에 영구 저장하고 Codex `thread-id`와 Discord Thread 간의 1:1 매핑을 유지한다.

핵심 흐름:

```text
Codex notify
   |
   v
NotifyServer validation
   |
   v
SessionManager
   |
   +--> SQLite transaction
   |      - workspace 등록/조회
   |      - session 등록/갱신
   |      - notify_events idempotency
   |
   +--> commit
   |
   +--> Discord Thread 생성 (필요한 경우에만)
   |
   +--> SQLite에 Discord Thread ID 저장
```

Discord API 호출은 SQLite transaction 안에서 수행하지 않는다.

---

## 2. SQLite schema

현재 schema version은 PHASE 9 migration 반영 후 `4`이며 `PRAGMA user_version`으로 관리한다. PHASE 4의 핵심 테이블에 PHASE 5의 `discord_deliveries`, PHASE 7의 persistent `jobs`, PHASE 9의 `sessions.auto_run`이 추가되어 있다.

### workspaces

- `id`
- `name`
- `path` UNIQUE
- `enabled`
- `created_at`
- `updated_at`

### sessions

- `thread_id` PRIMARY KEY
- `workspace_id`
- `client`
- `mode` (`LIVE` / `MANAGED`)
- `discord_thread_id` UNIQUE
- `status`
- `last_turn_id`
- `last_seen_at`
- `created_at`
- `updated_at`

### notify_events

- `id`
- `event_type`
- `thread_id`
- `turn_id`
- `received_at`
- `payload_hash`

영구 idempotency key:

```text
UNIQUE(thread_id, turn_id, event_type)
```

`payload_hash`는 prompt 또는 assistant result 본문을 포함하지 않는다. 이벤트 identity, workspace, client 메타데이터만 fingerprint한다.

### discord_deliveries (schema v2 / PHASE 5)

- `notify_event_id` PRIMARY KEY / FK
- `status`
- `attempts`
- `discord_message_id`
- `delivered_at`
- `last_error`
- `created_at`
- `updated_at`

자세한 delivery state machine은 `docs/completion-notifications.md`를 참고한다.

---

## 3. SQLite 운영 설정

DB open 시 다음을 적용한다.

```text
PRAGMA foreign_keys = ON
PRAGMA journal_mode = WAL
PRAGMA synchronous = NORMAL
PRAGMA busy_timeout = 5000
```

DB 파일 권한은 `0600`으로 조정한다.

기본 경로:

```text
/var/lib/codex-dispatch/codex-dispatch.db
```

설정:

```text
CODEX_DISPATCH_DB_PATH=/var/lib/codex-dispatch/codex-dispatch.db
```

---

## 4. Transaction 경계

`Database.register_notify()`는 다음 작업을 하나의 `BEGIN IMMEDIATE` transaction으로 수행한다.

1. workspace 조회 또는 생성
2. Codex session 조회 또는 생성
3. 기존 session의 last turn/status 갱신
4. `notify_events` insert-or-ignore
5. commit

Discord API 호출은 commit 이후에만 수행한다.

이 정책의 목적:

- 네트워크 지연 동안 SQLite write lock을 잡지 않음
- Discord 장애가 DB transaction rollback을 장시간 지연하지 않음
- notify event 자체는 Discord 장애와 별개로 영구 기록됨

---

## 5. Session 불변식

### Codex thread와 workspace

한 번 등록된 Codex `thread-id`는 다른 workspace로 자동 이동할 수 없다.

같은 `thread-id`가 다른 `cwd`로 들어오면:

```text
DatabaseInvariantError
```

로 거부한다.

### Discord Thread 매핑

- 한 Codex session은 최대 하나의 Discord Thread를 가진다.
- 한 Discord Thread는 최대 하나의 Codex session에만 매핑된다.
- 기존 Codex session을 다른 Discord Thread로 자동 remap하지 않는다.

이 규칙은 SQLite unique constraint와 application invariant 양쪽에서 검사한다.

---

## 6. Unknown session 등록

새 Codex notify가 들어오면 `SessionManager`는 자동으로:

1. workspace 저장
2. session 저장
3. notify event 저장
4. 세션별 lock 획득
5. Discord control channel 아래 Thread 생성
6. 생성된 Discord Thread ID를 session에 저장

Thread 이름 형식:

```text
<workspace-name> / <codex-thread-id-first-8>
```

Discord Thread name 최대 길이 100자를 넘지 않는다.

---

## 7. Discord Control Channel

PHASE 4부터 자동 Thread 생성 위치를 명시적으로 설정한다.

```text
DISCORD_CONTROL_CHANNEL_ID=<channel-id>
```

이 ID는 반드시 다음 allowlist에도 포함되어야 한다.

```text
DISCORD_ALLOWED_CHANNEL_IDS=<channel-id>[,...]
```

그렇지 않으면 startup configuration validation이 실패한다.

Discord control channel은 일반 text channel처럼 `create_thread`를 지원해야 한다.

---

## 8. 중복 notify

### 같은 프로세스 안

PHASE 3 `NotifyServer`의 bounded memory cache가 빠르게 차단한다.

### 서비스 재시작 후

메모리 cache는 사라지지만 SQLite `notify_events` UNIQUE constraint가 동일 이벤트를 다시 insert하지 않는다.

따라서:

```text
서비스 restart
   |
동일 notify replay
   |
notify_events INSERT OR IGNORE
   |
event_inserted = false
```

가 된다.

중복 이벤트라도 session에 Discord Thread 매핑이 없는 경우에는 Thread 생성만 재시도한다. 이는 Discord 장애 후 복구를 가능하게 한다.

---

## 9. Discord 장애 시 정책

DB 저장 성공 후 Discord Thread 생성이 실패하면:

```text
session persisted
notify persisted
discord_thread_id = NULL
```

상태로 남는다.

동일 notify 또는 이후 notify가 다시 들어오면 `SessionManager`가 매핑이 없는 것을 확인하고 Thread 생성을 다시 시도한다.

실패한 Discord API 호출 때문에 notify event를 중복 저장하지는 않는다.

---

## 10. Restart recovery

SQLite가 persistent storage이므로 서비스 restart 후:

- workspace 복구
- Codex session 복구
- Discord Thread ID 복구
- notify idempotency 복구

가 자동으로 이루어진다.

이미 Discord Thread가 매핑된 session의 notify가 다시 들어와도 새 Thread를 만들지 않는다.

---

## 11. 테스트

전체 테스트:

```bash
bash scripts/test.sh
```

PHASE 4 핵심 테스트에는 다음이 포함된다.

- schema 생성 및 version 확인
- database permission `0600`
- workspace/session/event 원자 등록
- persistent notify duplicate 방지
- session workspace 변경 차단
- Discord Thread unique mapping
- DB reopen 후 mapping 복구
- unknown session Thread 자동 생성
- 동일 session 후속 turn의 Thread 재사용
- 3 sessions -> 3 distinct Discord Threads
- Discord 장애 후 Thread mapping retry

---

## 12. Host Acceptance

실제 Codex Notify + Discord 환경에서 최소 3개의 서로 다른 Codex session을 발생시킨 후 다음을 실행한다.

```bash
cd /home/your-user/codex-dispatch

set -a
source /etc/codex-dispatch/codex-dispatch.env
set +a

PYTHONPATH=src python3 scripts/session-host-acceptance.py --minimum-sessions 3
```

PASS 예:

```text
schema_version=4
sessions_total=3
sessions_mapped=3
unique_discord_threads=3
Codex Dispatch PHASE 4 acceptance: PASS
```

서비스를 재시작하고 동일 명령을 다시 실행했을 때 같은 DB state가 유지되어야 한다.

---

## 13. PHASE 4 완료 기준

다음을 모두 만족하면 완전 PASS다.

- SQLite schema v2에서 PHASE 4 workspace/session/notify 상태가 정상 유지됨
- workspace/session/notify_events persistent 저장
- notify event 재생 시 중복 row 없음
- session별 Discord Thread 1:1 매핑
- 서로 다른 3개 Codex session이 서로 다른 Discord Thread에 매핑됨
- 서비스 restart 후 동일 mapping 유지
- Discord 장애 이후 unmapped session의 Thread 생성 재시도 가능
- 동일 Codex thread가 다른 workspace로 변경되는 경우 거부
