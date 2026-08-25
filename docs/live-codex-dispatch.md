# PHASE 6 - Discord to Live Codex Session Dispatch

## 1. 목적

PHASE 6는 Codex Dispatch의 양방향 제어 루프를 완성한다.

```text
Codex 완료
   |
   v
Discord Thread 완료 알림
   |
   | 사용자가 다음 작업 입력
   v
Discord Bot
   |
   v
DiscordPromptDispatcher
   |
   v
CodexQueueRunner
   |
   v
codex queue --thread <THREAD_ID> --message <PROMPT>
   |
   v
기존 LIVE Codex session
```

PHASE 7의 persistent job queue/idempotency가 들어오기 전 단계이므로, PHASE 6는 **하나의 허가된 Discord 메시지를 즉시 live Codex session에 전달하는 경로**에 집중한다.

---

## 2. Codex CLI 계약

현재 구현이 요구하는 contract:

```text
codex queue --thread <THREAD_ID> --message <PROMPT>
```

서비스 시작 시 한 번 다음을 실행해 capability를 확인한다.

```text
codex queue --help
```

다음을 모두 찾지 못하면 startup을 실패시킨다.

```text
--thread
--message
```

이 방식으로 오래된 Codex CLI에서 존재하지 않는 명령을 추측해서 호출하지 않는다.

PHASE 0 기준선에서 `codex queue`는 Codex CLI 0.149.0에 추가되었으며, 실제 VPS 설치 버전의 help output이 최종 contract다.

---

## 3. 설정

기본값:

```text
CODEX_DISPATCH_CODEX_BIN=codex
CODEX_DISPATCH_CODEX_QUEUE_TIMEOUT_SECONDS=10
CODEX_DISPATCH_CODEX_CAPABILITY_TIMEOUT_SECONDS=5
CODEX_DISPATCH_CODEX_PROMPT_MAX_CHARS=8000
```

systemd PATH에 npm global bin이 포함되지 않는 환경이면 `CODEX_DISPATCH_CODEX_BIN`에 실제 absolute path를 지정하는 것을 권장한다.

예:

```text
CODEX_DISPATCH_CODEX_BIN=/home/your-user/.npm-global/bin/codex
```

경로를 포함한 값은 absolute path만 허용한다.

---

## 4. Discord Thread -> Codex Session lookup

허가된 Discord 메시지가 들어오면 `channel_id`를 SQLite의 `sessions.discord_thread_id`와 비교한다.

```text
Discord Thread 7001
        |
        v
sessions.discord_thread_id = 7001
        |
        v
Codex thread-id
```

매핑되지 않은 control channel 또는 임의 Thread에서는 Codex 명령을 실행하지 않는다.

사용자에게 다음 오류를 반환한다.

```text
⚠️ 이 Discord Thread에 연결된 Codex 세션이 없습니다.
```

---

## 5. LIVE mode 제한

PHASE 6는 다음 세션만 처리한다.

```text
mode = LIVE
```

`MANAGED` session은 PHASE 8에서 `codex exec resume` 경로로 별도 구현한다.

따라서 PHASE 6에서 `LIVE`가 아닌 session에 메시지를 보내면 Codex subprocess를 시작하지 않는다.

---

## 6. Workspace

Codex queue subprocess의 `cwd`는 session이 등록될 때 저장한 workspace path를 사용한다.

이 path는 PHASE 3 notify validation을 통해 이미:

- absolute path
- existing directory
- realpath
- `CODEX_ALLOWED_ROOTS` 내부
- symlink escape 없음

조건을 만족한 값이다.

workspace row가 사라졌거나 disabled이면 dispatch를 차단한다.

---

## 7. Prompt validation

Discord prompt는 다음 처리를 거친다.

1. CRLF/CR을 LF로 정규화
2. 앞뒤 whitespace 제거
3. empty prompt 거부
4. NUL character 거부
5. `CODEX_DISPATCH_CODEX_PROMPT_MAX_CHARS` 초과 거부

내부 newline은 유지한다.

prompt body는 INFO 로그에 기록하지 않는다. 로그에는 `message_id`, `thread_id`, 길이만 기록한다.

---

## 8. Shell injection 방지

절대 다음 형태를 사용하지 않는다.

```text
shell=True
os.system(...)
create_subprocess_shell(...)
```

실제 실행은 argv 기반이다.

개념적 Python 호출:

```text
create_subprocess_exec(
    codex_binary,
    "queue",
    "--thread",
    thread_id,
    "--message",
    prompt,
    cwd=workspace,
)
```

Discord message는 shell command가 아니며 하나의 Codex prompt argument로 전달된다.

### process-list privacy note

Codex CLI의 현재 queue contract가 `--message <PROMPT>` argument를 요구하므로, 실행 시간이 짧더라도 같은 Unix host에서 다른 사용자가 process argv를 조회할 수 있는 환경에서는 prompt가 process list에 순간적으로 보일 가능성이 있다.

따라서 VPS는 신뢰 가능한 사용자만 shell access를 가지도록 운영해야 한다. 향후 Codex가 stdin/file-descriptor 기반 queue input을 제공하면 해당 방식을 우선 검토한다.

---

## 9. Timeout 및 subprocess 처리

queue command 기본 timeout:

```text
10 seconds
```

시간을 초과하면 subprocess를 kill하고 session status를 `ERROR`로 변경한다.

서비스 startup capability check 기본 timeout:

```text
5 seconds
```

Codex binary가 없거나 queue capability가 없으면 startup은 `CodexRunnerError`로 실패하고 process exit code는 `6`이다.

---

## 10. Session 상태

성공적인 queue 전달 후:

```text
RUNNING
```

으로 변경한다.

이후 Codex `agent-turn-complete` notify가 오면 기존 PHASE 4 `register_notify()`가 다시:

```text
IDLE
```

로 변경한다.

주요 실패 상태:

```text
Codex session/thread not found -> OFFLINE
queue timeout                  -> ERROR
queue generic failure          -> ERROR
queue capability missing       -> ERROR
workspace unavailable          -> ERROR
```

---

## 11. 사용자 피드백

queue command가 성공하면 같은 Discord Thread에:

```text
▶️ Codex에 전달했습니다.
```

를 보낸다.

이 acknowledgement 전송이 실패하더라도 이미 성공한 Codex queue 요청을 취소하거나 실패 처리하지 않는다.

Codex 작업이 실제 완료되면 PHASE 5 completion notification이 별도로 도착한다.

---

## 12. Live session unavailable 판정

`codex queue`가 non-zero로 종료할 때 stdout/stderr의 제한된 diagnostic 문자열을 내부적으로 검사한다.

예를 들어 다음 계열은 stale/offline session으로 분류한다.

```text
thread not found
session not found
unknown thread
unknown session
session is not running
thread is stopped
```

prompt/output 본문은 로그로 남기지 않는다.

그 외 non-zero는 generic queue rejection으로 처리한다.

---

## 13. PHASE 6 Host Acceptance

### 13.1 runner acceptance

실제 live Codex session이 있고 PHASE 4 DB에 해당 session이 등록되어 있어야 한다.

```bash
cd /home/your-user/codex-dispatch

set -a
source /etc/codex-dispatch/codex-dispatch.env
set +a

PYTHONPATH=src python3 \
  scripts/live-dispatch-host-acceptance.py \
  --thread <CODEX_THREAD_ID>
```

이 probe는 파일 변경을 요구하지 않고 다음 응답만 요청한다.

```text
CODEX_DISPATCH_PHASE6_OK
```

CLI queue command가 성공하면:

```text
Codex Dispatch PHASE 6 queue command: PASS
```

가 출력된다.

CLI exit code 0만으로 full E2E PASS는 아니다. 실제 target Codex session이 probe를 받고 응답해야 한다.

### 13.2 Discord full E2E acceptance

1. Codex Dispatch service 실행
2. PHASE 4에서 생성된 session Discord Thread 열기
3. 다음 메시지 입력

```text
파일을 수정하지 말고 정확히 CODEX_DISPATCH_PHASE6_OK 라고만 답해.
```

4. Discord에서 acknowledgement 확인

```text
▶️ Codex에 전달했습니다.
```

5. 기존 Codex TUI/session이 prompt를 수신하는지 확인
6. turn 완료 후 같은 Discord Thread에 PHASE 5 완료 알림이 오는지 확인
7. 결과가 `CODEX_DISPATCH_PHASE6_OK`인지 확인

최종 흐름:

```text
Discord prompt
   -> codex queue
   -> existing live Codex context
   -> agent-turn-complete
   -> Discord completion
```

전체가 성공해야 PHASE 6 host acceptance를 PASS로 판정한다.

---

## 14. PHASE 6에서 아직 보장하지 않는 것

다음은 PHASE 7 범위다.

- Discord message ID의 persistent idempotency
- 여러 inbound prompt의 durable job queue
- session별 application-level dispatch queue/lock
- `/queue`
- `/cancel`
- restart 후 queued job recovery

즉 Discord Gateway가 동일 user message event를 재전달하는 극단적인 상황의 영구 중복 방지는 PHASE 7의 `jobs` table에서 완성한다.

---

## 15. PHASE 6 완료 기준

코드 기준:

- Discord Thread -> session lookup 구현
- LIVE mode 검증
- prompt normalization/size validation
- workspace lookup
- `codex queue --thread --message` argv 실행
- startup capability detection
- timeout + process kill
- Codex binary missing 처리
- stale/offline session 분류
- success -> RUNNING
- completion notify -> IDLE 기존 경로 유지
- user acknowledgement/error feedback
- shell 실행 미사용

Host 기준:

- 실제 Codex 0.149.0+ 또는 동등 queue capability 확인
- Discord Thread prompt가 기존 live session에 전달됨
- 기존 session context가 유지됨
- 결과가 동일 Discord Thread completion으로 돌아옴
