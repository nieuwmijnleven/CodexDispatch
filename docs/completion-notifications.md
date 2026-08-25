# PHASE 5 - Codex Completion Notifications

## 목적

Codex `agent-turn-complete` 이벤트를 영구 session mapping과 연결하고, 해당 Codex session에 대응하는 Discord Thread에 작업 완료 결과를 한 번만 전달한다.

전체 흐름:

```text
Codex notify
    |
    v
NotifyServer
    |
    v
SessionManager
    |
    +-- workspace/session/notify event 저장
    +-- Discord Thread mapping 보장
    |
    v
CompletionNotifier
    |
    +-- discord_deliveries 상태 조회
    +-- Discord message 포맷
    +-- send/retry
    +-- Discord message ID 저장
    |
    v
Discord session Thread
```

---

## Discord 메시지 형식

기본 형식:

```text
✅ Codex 작업 완료

Project: creator-alliance
Session: abcdef12

Result:
<last-assistant-message>
```

표시 규칙:

- `Project`는 저장된 workspace 이름을 사용한다.
- `Session`은 Codex thread ID 앞 8자를 사용한다.
- 결과는 `last-assistant-message`를 사용한다.
- 결과가 없으면 `(결과 메시지 없음)`을 표시한다.
- Discord text message 2,000자 제한을 초과하면 결과 부분만 잘라내고 `… (truncated)`를 붙인다.
- NUL 문자는 제거한다.
- Discord Client는 `AllowedMentions.none()`을 기본값으로 사용하여 Codex 결과의 `@everyone`, role/user mention 문자열이 실제 알림으로 확장되지 않게 한다.

---

## DB schema v2

PHASE 5에서 completion outbox가 추가되며 schema version `2`가 도입되었다. 현재 전체 프로젝트 schema는 PHASE 9의 auto-run migration까지 반영되어 `4`이다.

새 테이블:

```text
discord_deliveries
------------------
notify_event_id        PRIMARY KEY / FK -> notify_events.id
status                 PENDING | SENDING | SENT | FAILED
attempts               send attempt count
discord_message_id     confirmed Discord message snowflake
delivered_at           confirmed delivery timestamp
last_error             bounded non-sensitive error summary
created_at
updated_at
```

`notify_events` 하나에 `discord_deliveries` 하나만 존재한다.

새 notify event를 등록하는 transaction에서 delivery row도 `PENDING`으로 함께 만든다. 기존 schema v1 DB는 open 시 v2로 migration되며 기존 notify event에는 `PENDING` delivery row가 생성된다.

---

## Delivery state machine

```text
              send success
PENDING ----------------------> SENT
   |                              ^
   | send attempt                 |
   v                              |
SENDING --------------------------+
   |
   | confirmed exception
   v
FAILED
   |
   | retry budget remains
   +-------------> SENDING
```

### PENDING

아직 Discord API 호출을 시작하지 않았다.

### SENDING

DB transaction으로 attempt를 먼저 기록한 뒤 외부 Discord API를 호출하는 상태다.

프로세스가 이 상태에서 종료되면 다음 시작 시 자동 retry하지 않는다. 외부 API가 실제 메시지를 생성했는지 여부가 모호할 수 있기 때문이다. 중복 메시지를 방지하기 위해 `SENDING`을 보수적으로 유지하고 운영자가 상태를 확인하도록 한다.

### SENT

Discord `send()`가 반환한 실제 message snowflake까지 DB에 저장된 상태다. 같은 notify event가 다시 들어와도 Discord API를 다시 호출하지 않는다.

### FAILED

Discord send가 예외로 명확하게 실패한 상태다. retry budget이 남아 있으면 exponential backoff 후 다시 시도한다.

---

## Retry 정책

환경 변수:

```text
CODEX_DISPATCH_DISCORD_RETRY_ATTEMPTS=3
CODEX_DISPATCH_DISCORD_RETRY_BASE_SECONDS=0.5
```

기본 delay:

```text
attempt 1 failure -> 0.5s
attempt 2 failure -> 1.0s
attempt 3 failure -> final FAILED
```

backoff는 최대 30초로 제한한다.

retry count는 process invocation마다 초기화되지 않고 DB의 `attempts`를 기준으로 누적된다. 따라서 동일 notify를 반복 전달해 retry budget을 무한히 초기화할 수 없다.

---

## 중복 방지 수준

정상적인 Codex duplicate notify, 동일 프로세스의 concurrent notify, 서비스 재시작 이후 이미 `SENT`로 확인된 event는 DB 상태를 통해 중복 Discord 메시지를 만들지 않는다.

Discord send가 성공했지만 응답을 받기 전에 네트워크가 끊기는 것처럼 외부 API 결과가 모호한 경우까지 분산 시스템 수준의 strict exactly-once를 보장할 수는 없다. Codex Dispatch는 다음 정책을 사용한다.

1. send 전 `SENDING` 상태를 durable commit한다.
2. 성공 응답에서 실제 Discord message ID를 받은 경우에만 `SENT`로 전환한다.
3. process interruption으로 `SENDING`이 남은 경우 자동 resend하지 않는다.
4. 동일 notify event에는 DB event ID를 Discord nonce로 전달해 추적 가능성을 높인다.

이 정책은 불확실한 상황에서 중복 메시지 방지를 우선한다.

---

## Transaction boundary

Discord network API를 호출하는 동안 SQLite write transaction을 유지하지 않는다.

```text
BEGIN IMMEDIATE
  delivery -> SENDING
  attempts += 1
COMMIT

Discord API send

BEGIN IMMEDIATE
  delivery -> SENT or FAILED
COMMIT
```

따라서 Discord latency가 SQLite lock을 장시간 점유하지 않는다.

---

## 보안 및 개인정보

- Bot token은 DB/log에 저장하지 않는다.
- prompt 본문은 DB에 저장하지 않는다.
- assistant 결과 본문은 DB에 저장하지 않는다.
- `last_error`에는 예외 type과 짧은 오류 문구만 최대 512자로 저장한다.
- Discord 결과 메시지는 mapped session Thread에만 보낸다.
- outbound channel은 기존 Discord allowlist 검증을 다시 통과해야 한다.
- Discord mention expansion은 기본 비활성화한다.

---

## 자동 테스트

주요 테스트 항목:

```text
[ ] project/session/result 포맷
[ ] 2,000자 정확한 truncate
[ ] 결과가 없는 notify placeholder
[ ] 성공 후 SENT + Discord message ID 저장
[ ] duplicate notify resend 방지
[ ] concurrent duplicate send 방지
[ ] transient failure retry/backoff
[ ] retry exhaustion -> FAILED
[ ] SENDING 상태 자동 resend 금지
[ ] schema v1 -> v2 migration
[ ] delivery 상태 재시작 persistence
```

전체 테스트:

```bash
bash scripts/test.sh
```

---

## Host Acceptance

실제 서비스가 실행 중인 상태에서 Codex turn 하나를 완료한다.

Discord에서 대응하는 session Thread에 다음과 같은 완료 메시지가 한 번 나타나는지 확인한다.

```text
✅ Codex 작업 완료

Project: <workspace>
Session: <short-id>

Result:
<result>
```

그 다음 VPS에서:

```bash
cd /home/ivenewjeans25/codex-dispatch
set -a
source /etc/codex-dispatch/codex-dispatch.env
set +a
PYTHONPATH=src python3 scripts/completion-host-acceptance.py --minimum-sent 1
```

PASS 예:

```text
schema_version=2
deliveries_total=1
deliveries_sent=1
deliveries_pending=0
deliveries_sending=0
deliveries_failed=0
latest_sent_event_id=1
latest_discord_message_id=123456789012345678
latest_attempts=1
Codex Dispatch PHASE 5 acceptance: PASS
```

실제 Discord에서 같은 Codex turn 완료 메시지가 한 번만 존재하는 것까지 확인하면 PHASE 5 host acceptance를 완료한다.
