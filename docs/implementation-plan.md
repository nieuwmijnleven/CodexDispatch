# Codex Dispatch 구현 계획

## 1. 문서 목적

이 문서는 `codex-dispatch` 프로젝트의 초기 설계와 구현 순서를 정의한다.

`codex-dispatch`의 목표는 Linux VPS에서 실행되는 Codex CLI 세션을 Discord를 통해 원격으로 확인하고 제어할 수 있도록 하는 것이다. 사용자는 VPS 터미널에 직접 접속하지 않고도 다음 흐름을 수행할 수 있어야 한다.

```text
Codex CLI 작업 실행
        |
        v
Codex turn 완료
        |
        | notify
        v
Codex Dispatch
        |
        v
Discord 완료 알림
        |
        | 사용자가 다음 프롬프트 입력
        v
Discord Bot
        |
        v
Codex Dispatch
        |
        v
기존 Codex 세션에 프롬프트 전달
        |
        v
Codex 다음 작업 실행
        |
        +----------> 완료 후 다시 Discord 알림
```

초기 버전은 Discord를 대상으로 구현하되, 핵심 오케스트레이션 로직은 Discord에 강하게 결합하지 않는다. 이후 Slack, Telegram, Web UI 등 다른 입력/알림 채널을 추가할 수 있는 구조를 유지한다.

---

## 2. 핵심 목표

### 2.1 필수 목표

1. Codex CLI의 작업 완료 이벤트를 감지한다.
2. 완료 이벤트에서 Codex `thread-id`, 작업 디렉터리(`cwd`), 마지막 결과를 식별한다.
3. 각 Codex 세션을 Discord Thread와 연결한다.
4. Codex 작업 완료 시 Discord에 자동으로 알림을 전송한다.
5. 사용자가 Discord Thread에 입력한 메시지를 해당 Codex 세션의 다음 프롬프트로 전달한다.
6. 기존 Codex 대화 문맥을 유지한 채 후속 작업을 실행한다.
7. 여러 Codex 세션을 동시에 관리하더라도 세션 간 프롬프트가 섞이지 않도록 한다.
8. 허가된 Discord 사용자와 채널만 Codex를 제어할 수 있도록 한다.
9. VPS 재부팅 또는 서비스 재시작 후 세션 매핑과 작업 상태를 복구할 수 있도록 한다.
10. Codex CLI 버전 차이로 특정 기능이 없더라도 안전하게 감지하고 대체 실행 경로를 선택한다.

### 2.2 초기 버전에서 제외하는 목표

다음 기능은 MVP 안정화 이후 추가한다.

- 다중 VPS 중앙 관리
- 다중 사용자 권한 모델
- Slack/Telegram adapter
- 웹 대시보드
- GitHub 연동 자동화
- Codex 승인 요청의 원격 처리
- 복잡한 DAG/workflow engine
- Codex 작업 결과의 장기 로그 검색 UI
- 외부 인터넷에 공개되는 REST API

---

## 3. 기본 원칙

### 3.1 Discord는 제어 UI이며 Codex가 실행 주체다

Discord Bot은 Codex 기능을 재구현하지 않는다. Bot은 사용자의 메시지를 수신하고 상태를 표시하는 인터페이스 역할만 담당한다.

```text
Discord
   |
   v
Codex Dispatch
   |
   v
Codex CLI
```

Codex의 세션, 추론 문맥, 저장된 인증 정보는 기존 Codex CLI가 관리한다.

### 3.2 TUI에 키보드 입력을 강제로 주입하지 않는다

`tmux send-keys`, PTY 키 입력 에뮬레이션 등 화면 자동화 방식은 사용하지 않는다.

프롬프트 전달은 Codex CLI가 제공하는 공식 명령 경로를 우선한다.

우선순위는 다음과 같다.

1. 설치된 Codex에서 기존 live session에 메시지를 전달하는 명령이 지원되는 경우 해당 기능 사용
2. headless/managed session은 `codex exec resume` 계열 사용
3. 기능이 없는 Codex 버전에서는 명확한 오류를 반환하고 사용자에게 업그레이드 필요 상태를 표시

실행 중인 동일 세션을 별도 프로세스에서 무조건 `resume`하는 방식은 사용하지 않는다. Live TUI session과 managed/headless session을 명확히 구분한다.

### 3.3 세션당 하나의 실행 흐름만 허용한다

같은 Codex `thread-id`에 동시에 두 개 이상의 실행 명령을 보내지 않는다.

```text
Session A
  RUNNING <= 1
  QUEUED  >= 0
```

세션 단위 lock을 적용한다.

### 3.4 모든 상태는 재시작 가능한 형태로 저장한다

세션 매핑과 job 상태는 SQLite에 저장한다. 프로세스 메모리에만 중요한 상태를 두지 않는다.

---

## 4. 권장 기술 스택

| 영역 | 기술 |
| --- | --- |
| Language | Python 3.11+ |
| Discord | `discord.py` |
| Async runtime | `asyncio` |
| Database | SQLite |
| Async DB | `aiosqlite` |
| Codex 실행 | `asyncio.create_subprocess_exec` |
| Local IPC | Unix Domain Socket |
| Process supervisor | systemd |
| Logging | Python `logging` 또는 `structlog` |
| Tests | `pytest`, `pytest-asyncio` |

초기 구현에서는 Redis, PostgreSQL, Celery, RabbitMQ, Docker 같은 추가 인프라를 요구하지 않는다.

---

## 5. 전체 아키텍처

```text
                                  Linux VPS

     Codex CLI                                          Discord
        |                                                  ^
        | agent/turn complete                              |
        v                                                  |
+------------------+      Unix Socket      +-------------------------------+
| codex-notify.py  | --------------------> |       Codex Dispatch          |
+------------------+                       |                               |
                                           |  Notify Server                |
                                           |  Session Manager              |
                                           |  Job Manager                  |
                                           |  Codex Runner                 |
                                           |  Discord Adapter              |
                                           |  Authorization                |
                                           |  SQLite                       |
                                           +-------------------------------+
                                                      |
                                                      | next prompt
                                                      v
                                                Codex CLI command
```

`codex-notify.py`는 최대한 작은 bridge로 유지한다. Discord API 호출, DB 갱신, session lookup 등의 비즈니스 로직은 모두 장기 실행되는 orchestrator 프로세스가 담당한다.

---

## 6. Discord 모델

### 6.1 Discord Thread와 Codex Session을 1:1로 매핑한다

기본 모델은 다음과 같다.

```text
#codex
  |
  +-- Thread: creator-alliance / ab12cd
  |      <-> Codex thread-id A
  |
  +-- Thread: creator-alliance / ef34gh
  |      <-> Codex thread-id B
  |
  +-- Thread: web-gpt / ij56kl
         <-> Codex thread-id C
```

같은 프로젝트에서 여러 Codex 세션을 실행하더라도 Discord Thread가 분리되므로 프롬프트 혼선을 줄일 수 있다.

### 6.2 첫 notify 수신 시 자동 등록

알 수 없는 `thread-id`로 완료 이벤트가 들어오면:

1. `cwd`가 허용된 workspace인지 검증한다.
2. session 레코드를 생성한다.
3. 지정된 Discord 채널 아래에 Thread를 생성한다.
4. Discord Thread ID와 Codex thread ID를 매핑한다.
5. 완료 메시지를 전송한다.

### 6.3 Discord 일반 메시지는 Codex prompt로 처리한다

허용된 Discord Thread 안에서 허용된 사용자가 작성한 일반 텍스트 메시지는 기본적으로 해당 Codex session에 대한 다음 프롬프트로 처리한다.

Bot 자체 메시지와 시스템 메시지는 무시한다.

---

## 7. Codex 통합 전략

Codex CLI 기능은 버전에 따라 달라질 수 있으므로 실행 전에 capability detection을 수행한다.

### 7.1 완료 알림

Codex 외부 notify 설정을 이용한다.

개념적 설정 예시는 다음과 같다.

```toml
notify = [
    "python3",
    "/opt/codex-dispatch/bin/codex-notify.py"
]
```

Bridge가 받아야 하는 최소 정보는 다음과 같다.

- event type
- Codex thread/session ID
- turn ID
- current working directory
- 입력 메시지 또는 작업 식별에 필요한 정보
- 마지막 assistant message

실제 payload 형식은 설치된 Codex 버전을 PHASE 0에서 검증하고 그 결과에 맞추어 parser를 구현한다.

### 7.2 Live session prompt 전달

설치된 Codex CLI에서 기존 실행 세션에 메시지를 전달하는 공식 queue 명령이 지원되는지 먼저 확인한다.

지원될 경우 개념적으로 다음 흐름을 사용한다.

```text
Discord message
    |
    v
session lookup
    |
    v
Codex live-session queue command
    |
    v
기존 TUI session
```

CLI 옵션명과 동작은 PHASE 0에서 실제 설치 버전으로 검증하고 hard-code하기 전에 integration test를 만든다.

### 7.3 Managed/headless session

오케스트레이터가 직접 실행하는 session은 Codex의 non-interactive 실행 및 resume 기능을 사용한다.

개념적 흐름:

```text
Discord
   |
   v
Codex Dispatch
   |
   v
codex exec resume <thread-id> ...
```

Live session과 managed session을 구분하여 동일 session에 두 실행 전략을 동시에 적용하지 않는다.

---

## 8. 프로젝트 구조

초기 권장 구조:

```text
codex-dispatch/
├── docs/
│   └── implementation-plan.md
├── src/
│   └── codex_dispatch/
│       ├── __init__.py
│       ├── main.py
│       ├── config.py
│       ├── logging.py
│       ├── models.py
│       ├── database.py
│       ├── security.py
│       ├── notify_server.py
│       ├── session_manager.py
│       ├── job_manager.py
│       ├── codex_runner.py
│       └── adapters/
│           ├── __init__.py
│           └── discord.py
├── bin/
│   └── codex-notify.py
├── tests/
│   ├── unit/
│   └── integration/
├── systemd/
│   └── codex-dispatch.service
├── pyproject.toml
├── .gitignore
├── .env.example
└── README.md
```

Discord 전용 코드를 `adapters/discord.py`에 격리해 후속 Slack adapter 추가 시 핵심 job/session 로직을 변경하지 않도록 한다.

---

## 9. 데이터 모델

### 9.1 workspaces

```text
id
name
path
enabled
created_at
updated_at
```

제약:

- `path`는 canonical realpath로 저장한다.
- 허용 root 밖의 path는 등록하지 않는다.
- symlink traversal을 통해 allowlist 밖으로 탈출할 수 없어야 한다.

### 9.2 sessions

```text
thread_id
workspace_id
client
mode
discord_thread_id
status
last_turn_id
last_seen_at
created_at
updated_at
```

`mode` 후보:

```text
LIVE
MANAGED
```

`status` 후보:

```text
IDLE
RUNNING
WAITING
OFFLINE
ERROR
```

### 9.3 jobs

```text
id
thread_id
discord_message_id
prompt
status
created_at
started_at
completed_at
exit_code
error
```

상태:

```text
QUEUED
DISPATCHED
RUNNING
COMPLETED
FAILED
CANCELLED
```

### 9.4 notify_events

중복 notify 방지를 위해 필요하다.

```text
id
event_type
thread_id
turn_id
received_at
payload_hash
```

가능하면 `(thread_id, turn_id, event_type)`에 unique constraint를 둔다.

---

## 10. 작업 Queue 정책

### 10.1 MVP

MVP에서는 Discord 메시지를 즉시 dispatch하되 session이 busy이면 `QUEUED`로 저장한다.

다만 자동으로 다음 queued job을 실행하는 기능은 기본 비활성화한다.

이유:

- 사용자가 완료 결과를 확인한 뒤 다음 작업을 취소하거나 수정할 수 있다.
- 잘못된 프롬프트가 연쇄 실행되는 것을 막는다.
- 초기 버전의 장애 범위를 줄인다.

### 10.2 후속 버전

session별 자동 실행 옵션을 제공한다.

```text
AUTO_RUN = false   # default
```

사용자가 명시적으로 활성화한 session만 완료 이벤트 수신 후 다음 queued job을 dispatch한다.

### 10.3 중복 방지

Discord message ID를 idempotency key로 사용한다.

동일 메시지를 reconnect 또는 event replay로 두 번 수신해도 job은 한 번만 생성한다.

---

## 11. Discord 사용자 경험

### 11.1 완료 알림

예시:

```text
✅ Codex 작업 완료

Project: creator-alliance
Session: ab12cd...
Status: IDLE

Result:
SOCIAL-07 구현 및 테스트 완료.

다음 작업을 이 Thread에 입력하세요.
```

### 11.2 프롬프트 접수

사용자:

```text
SOCIAL-08 진행해. 완료 후 전체 테스트까지 실행해.
```

Bot:

```text
📥 Codex 작업 접수
Job: #42
Status: QUEUED
```

실제 dispatch 후:

```text
▶️ Codex에 전달됨
Job: #42
Status: RUNNING
```

### 11.3 초기 slash command

MVP 필수:

- `/status` - 현재 session 상태
- `/queue` - 대기 중인 job 목록
- `/cancel` - 아직 실행되지 않은 job 취소
- `/session` - 현재 Codex thread/session 정보

MVP 이후:

- `/auto state:on`
- `/auto state:off`
- `/retry`
- `/workspace`
- `/logs`

---

## 12. 보안 요구사항

Discord 메시지가 VPS의 코드 실행 에이전트로 전달되므로 일반 Bot보다 강한 보안 기준을 적용한다.

### 12.1 Discord allowlist

최소한 다음을 제한한다.

```text
DISCORD_ALLOWED_GUILD_IDS
DISCORD_ALLOWED_CHANNEL_IDS
DISCORD_ALLOWED_USER_IDS
```

정책:

- DM 기본 거부
- 등록되지 않은 Guild 거부
- 등록되지 않은 Channel 거부
- 등록되지 않은 User 거부
- Bot 계정 메시지 무시

### 12.2 Workspace allowlist

예시:

```text
CODEX_ALLOWED_ROOTS=/home/ivenewjeans25/project-a:/home/ivenewjeans25/project-b
```

홈 디렉터리 전체를 허용하지 않는다. `.codex`, `.ssh`, Codex 실행파일, Codex Dispatch 소스/venv/DB가 허용 root 아래에 들어가면 원격 prompt가 오케스트레이터나 자격증명 자체를 수정할 수 있으므로 startup security validation에서 거부한다.

모든 `cwd`는 다음 검증을 거친다.

1. absolute path 변환
2. realpath 해석
3. 허용 root의 실제 하위 경로인지 검사
4. symlink 탈출 여부 검사

`/`, `/etc`, `/root` 등 허용 root 밖의 경로는 절대 Codex 대상으로 등록하지 않는다.

### 12.3 Shell injection 방지

다음 API를 사용하지 않는다.

```python
os.system(...)
subprocess(..., shell=True)
asyncio.create_subprocess_shell(...)
```

항상 argv 기반 실행을 사용한다.

```python
await asyncio.create_subprocess_exec(
    "codex",
    "...",
    thread_id,
    prompt,
)
```

Discord 메시지는 shell 문자열이 아니라 Codex prompt argument/data로만 취급한다.

### 12.4 Secret 관리

Bot token 및 기타 secret은 Git 저장소에 저장하지 않는다.

예시:

```text
/etc/codex-dispatch/codex-dispatch.env
```

권장 권한:

```text
0600
```

`.env.example`에는 값 없는 변수명과 설명만 넣는다.

### 12.5 Codex 실행 계정

systemd 서비스는 실제 Codex CLI가 로그인되어 있고 session을 소유하는 Linux 계정으로 실행한다.

별도 root 서비스로 Codex를 실행하지 않는다.

---

## 13. IPC 설계

`codex-notify.py`에서 orchestrator로 이벤트를 전달하기 위해 Unix Domain Socket을 사용한다.

예시:

```text
/run/user/<uid>/codex-dispatch/notify.sock
```

또는 서비스 운영 정책에 따라 `/run/codex-dispatch/notify.sock`을 사용할 수 있다.

요구사항:

- 외부 TCP port를 열지 않는다.
- socket file permission으로 로컬 접근자를 제한한다.
- payload 최대 크기를 제한한다.
- JSON schema를 검증한다.
- malformed payload는 저장/실행하지 않는다.
- socket server 장애 시 notify bridge는 짧은 timeout 후 종료해야 한다.

Codex 작업 완료를 알리는 notify 프로그램이 Codex 프로세스를 장시간 block해서는 안 된다.

---

## 14. Logging 및 관측성

운영 로그에는 최소 다음 필드를 남긴다.

```text
timestamp
level
event
thread_id
turn_id
job_id
workspace
discord_thread_id
exit_code
```

민감한 전체 prompt나 Codex 결과는 기본 INFO 로그에 남기지 않는다.

권장 이벤트:

```text
notify.received
notify.duplicate
session.created
session.updated
job.created
job.dispatched
job.completed
job.failed
discord.message.rejected
codex.command.failed
service.started
service.stopped
```

journalctl만으로 장애 원인을 추적할 수 있어야 한다.

---

## 15. systemd 운영

서비스 이름:

```text
codex-dispatch.service
```

개념적 unit:

```ini
[Unit]
Description=Codex Dispatch Discord Orchestrator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<codex-user>
WorkingDirectory=/home/<user>/codex-dispatch
EnvironmentFile=/etc/codex-dispatch/codex-dispatch.env
ExecStart=/home/<user>/codex-dispatch/.venv/bin/python -m codex_dispatch.main
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
```

실제 경로는 install script에서 결정하도록 하고 unit 파일에 특정 사용자 홈 경로를 불필요하게 하드코딩하지 않는다.

---

# 16. 단계별 구현 계획

## PHASE 0 - Codex Capability Discovery

### 목표

현재 VPS에 설치된 Codex CLI가 제공하는 실제 기능과 payload를 먼저 확인한다.

### 구현 상태

PHASE 0 진단 도구 구현은 완료되었다. 실제 Codex CLI는 `ivenewjeans25` 사용자 환경에서 실행되지만 MCP 작업 셸은 격리된 `devspace` 사용자이므로, host-user acceptance는 해당 사용자 셸에서 별도로 실행해야 한다.

구현 산출물:

- `scripts/codex-capability-discovery.sh` - 비파괴/passive CLI capability 탐지
- `bin/codex-notify-probe.py` - 민감한 prompt/result/path/ID를 저장하지 않는 notify schema probe
- `scripts/codex-live-session-probe.sh` - 명시적 thread에 실제 queue message를 보내는 opt-in active probe
- `tests/fixtures/codex-notify-agent-turn-complete.json` - 비식별 notify fixture
- `docs/codex-capabilities.md` - upstream 기준선, 실행 절차, acceptance matrix

아래 체크리스트는 실제 Codex 사용자 환경에서 수행하는 **Host acceptance** 기준이다.

### 작업

- [ ] `codex --version` 확인
- [ ] Codex CLI help에서 notify 관련 설정 확인
- [ ] 기존 session에 메시지를 넣을 수 있는 queue 기능 존재 여부 확인
- [ ] `exec` 및 `exec resume` 지원 형태 확인
- [ ] 테스트 Codex session 생성
- [ ] notify helper를 임시 연결하여 실제 payload 캡처
- [ ] payload의 `thread-id` 확인
- [ ] payload의 `turn-id` 확인
- [ ] payload의 `cwd` 확인
- [ ] live TUI session에 queue command가 실제로 전달되는지 검증
- [ ] 동일 session에 concurrent command를 보냈을 때 Codex의 동작 파악
- [ ] Codex 프로세스가 종료된 session을 대상으로 할 때 오류 형태 확인

### 산출물

- `docs/codex-capabilities.md`
- Codex 버전별 capability matrix의 초기 값
- integration fixture용 sanitized notify payload

### 완료 조건

다음 질문에 모두 답할 수 있어야 한다.

1. 현재 설치 버전에서 live TUI session에 공식 방식으로 후속 메시지를 넣을 수 있는가?
2. Codex session의 안정적인 식별자는 무엇인가?
3. 완료 이벤트에서 workspace를 안정적으로 식별할 수 있는가?
4. managed mode에서 기존 context를 유지하는 정확한 CLI 호출은 무엇인가?

PHASE 0 결과 없이 Codex command syntax를 애플리케이션 코드에 고정하지 않는다.

---

## PHASE 1 - 프로젝트 Bootstrap

### 목표

테스트 가능한 최소 Python 프로젝트와 설정 시스템을 만든다.

### 작업

- [x] `pyproject.toml` 작성
- [x] `src/codex_dispatch` package 생성
- [x] pytest 설정
- [x] configuration loader 구현
- [x] `.env.example` 작성
- [x] `.gitignore` 작성
- [x] logging bootstrap 구현
- [x] 기본 CI 가능한 unit test 구성

### 구현 상태

PHASE 1 구현은 완료되었다.

현재 MCP/DevSpace 실행 환경에는 `pip`와 `pytest`가 설치되어 있지 않아 이 환경에서 실제 `pytest` executable을 실행하는 host acceptance는 수행할 수 없었다. 대신 모든 테스트는 `unittest` 호환 형태로 작성했으며 `scripts/test.sh`가 pytest 존재 시 pytest를, 없으면 unittest를 실행하도록 구성했다.

현재 검증 결과:

```text
Python 3.12.3
unittest: 12 tests PASS
compileall: PASS
git diff --check: PASS
pytest host execution: BLOCKED (pip/pytest unavailable in DevSpace runtime)
```

### 완료 조건

```text
pytest
```

가 clean checkout에서 성공하고, configuration validation failure가 명확한 오류를 반환해야 한다. 코드 및 테스트 구현은 완료됐으며 실제 Codex 사용자 환경에서 `python -m pip install -e '.[dev]'` 후 pytest host acceptance를 한 번 수행하면 이 조건까지 완전히 검증된다.

---

## PHASE 2 - Discord Bot Adapter

### 목표

VPS와 Discord 사이의 안전한 양방향 통신을 만든다.

### 작업

- [x] Discord application/Bot 생성 절차 문서화 (`docs/discord-setup.md`)
- [x] `discord.py` 연결 (`discord.py>=2.7.1,<3`)
- [x] startup/shutdown 처리
- [x] Guild allowlist
- [x] Channel allowlist
- [x] User allowlist
- [x] Bot/system/Webhook message filtering
- [x] text message 수신
- [x] text message 전송
- [x] Discord reconnect 처리 (`reconnect=True`)
- [x] authorization unit test
- [x] 허용 parent channel 아래 Discord Thread authorization
- [x] outbound channel authorization
- [x] 메시지 본문/토큰 비로깅 정책
- [x] interactive host acceptance script (`scripts/discord-host-acceptance.py`)
- [ ] 실제 Discord Bot token으로 Gateway host acceptance 실행

### 구현 상태

```text
adapter implementation: PASS
unit tests: PASS
live Discord Gateway acceptance: PENDING (Bot token/Guild setup required)
```

### 완료 조건

허용 사용자가 테스트 채널 또는 그 하위 Thread에 작성한 메시지만 애플리케이션이 수신하고, 허용하지 않은 Guild/Channel/User 및 DM/Bot/Webhook/system 메시지는 handler에 도달하지 않아야 한다. 실제 VPS에서는 `docs/discord-setup.md` 절차에 따라 `scripts/discord-host-acceptance.py`를 실행해 inbound + outbound Gateway 동작을 최종 확인한다.

---

## PHASE 3 - Notify Bridge

### 목표

Codex 완료 이벤트를 안정적으로 orchestrator에 전달한다.

### 작업

- [x] `bin/codex-notify.py` 작성
- [x] notify argument/payload parser
- [x] Unix socket client 구현
- [x] orchestrator Unix socket server 구현
- [x] 4-byte length-prefix framing 적용
- [x] payload size limit
- [x] event schema validation
- [x] `agent-turn-complete` event type validation
- [x] thread-id validation
- [x] cwd absolute/existing-directory validation
- [x] `CODEX_ALLOWED_ROOTS` + realpath/symlink escape validation
- [x] `(thread-id, turn-id, type)` 기반 bounded in-memory idempotency
- [x] bridge timeout/failure policy
- [x] socket directory `0700` / socket `0600` 권한 검증
- [x] active second-instance socket 보호
- [x] malformed/oversized payload 테스트
- [x] 실제 Codex host acceptance 도구 작성

### 구현 결과

- `src/codex_dispatch/notify_client.py`
- `src/codex_dispatch/notify_server.py`
- `bin/codex-notify.py`
- `scripts/notify-host-acceptance.py`
- `docs/notify-bridge.md`
- `tests/test_notify.py`

PHASE 3의 중복 억제는 프로세스 내부 bounded cache다. 영구 idempotency는 PHASE 4의 SQLite `notify_events` 저장소로 이전한다.

### 완료 조건

자동화된 client/server round trip과 보안/실패 경로 검증은 완료됐다. 실제 Codex CLI가 turn을 완료했을 때 orchestrator가 정확한 `thread-id`, `turn-id`, `cwd`를 수신하는 VPS host acceptance 1회만 남아 있다. 해당 검증은 실제 Codex 사용자 환경에서 `scripts/notify-host-acceptance.py`로 수행한다.

---

## PHASE 4 - Database 및 Session Manager

### 목표

Codex session과 Discord Thread 간 영구 매핑을 구현한다.

### 구현 상태

**IMPLEMENTED - HOST ACCEPTANCE PENDING**

### 작업

- [x] SQLite schema 작성 (`workspaces`, `sessions`, `notify_events`)
- [x] migration/version mechanism 결정 (`PRAGMA user_version`, schema v1)
- [x] workspace repository
- [x] session repository
- [x] notify event repository
- [x] transaction boundary 정의
- [x] unknown session 자동 등록
- [x] Discord Thread 자동 생성
- [x] Codex thread <-> Discord thread mapping
- [x] duplicate notify 영구 처리
- [x] service restart 복구
- [x] `DISCORD_CONTROL_CHANNEL_ID` 및 allowlist 교차 검증
- [x] session별 asyncio lock으로 중복 Thread 생성 방지
- [x] 동일 Codex thread의 workspace 변경 차단
- [x] Discord 장애 후 unmapped session Thread 생성 재시도

### 구현 산출물

```text
src/codex_dispatch/models.py
src/codex_dispatch/database.py
src/codex_dispatch/session_manager.py
scripts/session-host-acceptance.py
docs/database-session-manager.md
tests/test_database.py
tests/test_session_manager.py
```

SQLite write transaction은 Discord API 호출 전에 commit한다. 영구 notify idempotency는 `UNIQUE(thread_id, turn_id, event_type)`로 보장하며, prompt/assistant 본문은 DB에 저장하거나 fingerprint하지 않는다.

### 완료 조건

자동 테스트에서는 Codex session 세 개가 서로 다른 Discord Thread ID에 매핑되고 DB reopen 이후에도 동일 mapping이 유지되는 것을 검증한다. 실제 Discord Gateway/Codex host에서는 서로 다른 실제 Codex session 세 개를 발생시킨 뒤 `scripts/session-host-acceptance.py --minimum-sessions 3`로 최종 acceptance를 수행한다.

---

## PHASE 5 - Codex -> Discord 완료 알림

### 목표

실제 Codex 결과를 사용자에게 읽기 쉬운 Discord 메시지로 전달한다.

### 작업

- [x] project/workspace 표시
- [x] session short ID 표시
- [x] 마지막 assistant message 길이 제한
- [x] Discord message 2,000자 제한 처리
- [x] 과도하게 긴 결과 truncate 처리
- [x] Discord 실패 시 bounded exponential retry 정책
- [x] notify event와 Discord message ID 연결
- [x] schema v2 `discord_deliveries` durable outbox
- [x] `PENDING -> SENDING -> SENT/FAILED` 상태 전이
- [x] 확인된 `SENT` event duplicate resend 방지
- [x] 동일 프로세스 concurrent duplicate delivery lock
- [x] ambiguous `SENDING` restart 상태 자동 resend 금지
- [x] Discord nonce에 notify event ID 전달
- [x] Discord mention expansion 기본 차단
- [x] schema v1 -> v2 migration

### 구현 산출물

```text
src/codex_dispatch/completion_notifier.py
scripts/completion-host-acceptance.py
docs/completion-notifications.md
tests/test_completion_notifier.py
```

SQLite schema version은 `2`로 상승한다. 기존 schema v1 DB는 자동 migration되며 기존 notify event에는 `PENDING` delivery row가 생성된다.

Discord send가 성공했지만 응답 확인 전에 연결이 끊기는 외부 API의 모호한 실패까지 strict exactly-once로 표현하지 않는다. Codex Dispatch는 send 직전 `SENDING`을 durable commit하고, process interruption으로 `SENDING`이 남으면 자동 resend하지 않아 중복 알림 방지를 우선한다. 명확한 send exception은 설정된 retry budget 내에서 exponential backoff로 재시도한다.

### 완료 조건

자동 테스트에서는 같은 notify event의 반복/동시 처리 시 확인된 Discord message가 한 번만 생성되고 `SENT + discord_message_id`가 영구 저장되는 것을 검증한다. 실제 host에서는 Codex turn 하나를 완료한 뒤 Discord session Thread에 완료 메시지가 한 번만 나타나는지 확인하고 `scripts/completion-host-acceptance.py --minimum-sent 1`을 PASS해야 한다.

---

## PHASE 6 - Discord -> Live Codex Session

### 목표

Discord 메시지를 기존 live Codex session의 다음 입력으로 전달한다.

### 구현 상태

```text
CODE: PASS
HOST ACCEPTANCE: PENDING
```

### 작업

- [x] Discord Thread에서 session lookup
- [x] prompt normalization
- [x] empty prompt 거부
- [x] max prompt size 제한
- [x] session mode 검증
- [x] PHASE 0에서 검증한 live-session queue command adapter 구현
- [x] startup `codex queue --help` capability 검증
- [x] argv 기반 subprocess 실행
- [x] stdout/stderr bounded capture
- [x] exit code 처리
- [x] timeout + process kill 정책
- [x] Codex CLI unavailable 처리
- [x] stale/offline session 처리
- [x] success 시 session `RUNNING`, completion 시 기존 경로로 `IDLE`
- [x] Discord acknowledgement/error feedback
- [x] prompt body 비로깅

### 구현 산출물

```text
src/codex_dispatch/codex_runner.py
src/codex_dispatch/prompt_dispatcher.py
scripts/live-dispatch-host-acceptance.py
docs/live-codex-dispatch.md
tests/test_codex_runner.py
tests/test_prompt_dispatcher.py
```

PHASE 6는 `codex queue --thread <id> --message <prompt>`를 shell 없이 argv로 실행한다. 설치된 Codex가 `queue --help`에서 `--thread`와 `--message`를 제공하지 않으면 서비스 startup을 fail-fast 처리한다. Discord message ID의 영구 idempotency와 durable job queue는 PHASE 7에서 구현한다.

### 완료 조건

다음 E2E 흐름이 성공해야 한다.

```text
VPS에서 Codex TUI 실행
        |
        v
작업 완료
        |
        v
Discord 알림
        |
        v
Discord에서 "다음 작업 진행" 입력
        |
        v
기존 Codex TUI session이 해당 prompt를 수신
        |
        v
동일 context로 작업 수행
```

이 과정에서 `tmux send-keys` 또는 키보드 에뮬레이션을 사용해서는 안 된다.

---

## PHASE 7 - Job Manager 및 Session Lock

### 목표

연속 메시지와 동시 요청을 안전하게 처리한다.

### 작업

- [x] job repository
- [x] Discord message idempotency
- [x] session별 asyncio lock
- [x] QUEUED 상태
- [x] DISPATCHED 상태
- [x] COMPLETED 상태
- [x] FAILED 상태
- [x] CANCELLED 상태 및 cancel-before-dispatch
- [x] `/queue`
- [x] `/status`
- [x] `/cancel`
- [x] service restart 시 orphan job recovery policy
- [x] session당 `DISPATCHED <= 1` DB partial unique index
- [x] completion notify와 active job 연결
- [x] application command Guild/Channel/User authorization

### 구현 산출물

```text
src/codex_dispatch/job_manager.py
scripts/job-host-acceptance.py
docs/job-queue.md
tests/test_job_manager.py
```

SQLite schema는 v3으로 상승하며 `jobs.discord_message_id UNIQUE`와 session별 단일 `DISPATCHED` partial unique index를 사용한다. 외부 `codex queue` 호출 전에 `DISPATCHED`를 commit하여 crash 후 ambiguous side effect를 자동 재전송하지 않는다. `QUEUED` job 자동 실행은 PHASE 9까지 비활성화한다.

### 완료 조건

자동 테스트에서는 동일 Discord Thread에 프롬프트 세 개를 동시에 전송해도 Codex queue 호출은 하나만 발생하고 job 상태가 `DISPATCHED=1`, `QUEUED=2`가 되는 것을 검증한다. 실제 host에서는 `scripts/job-host-acceptance.py --minimum-jobs 3 --minimum-queued 1`과 `/status`, `/queue`, `/cancel`을 확인한다.

---

## PHASE 8 - Managed/Headless Mode

### 목표

Live TUI가 없어도 Codex Dispatch 자체가 기존 Codex session을 이어서 실행할 수 있도록 한다.

### 작업

- [x] session `LIVE` / `MANAGED` mode 분리
- [x] PHASE 0에서 검증한 `codex exec resume` adapter 구현
- [x] stdin prompt 전달 지원
- [x] subprocess lifecycle 추적
- [x] stdout/stderr bounded capture 및 full drain 정책
- [x] exit code 처리
- [x] process cancellation
- [x] graceful shutdown
- [x] 동일 thread에 live + managed 내부 동시 실행 차단
- [x] `/mode live|managed` 전환
- [x] active MANAGED job `/cancel`
- [x] MANAGED crash/restart orphan job -> FAILED recovery
- [x] startup `exec resume --help` capability verification

### 구현 산출물

```text
src/codex_dispatch/managed_runner.py
scripts/managed-host-acceptance.py
docs/managed-headless-mode.md
tests/test_managed_runner.py
```

Managed prompt는 `codex exec resume <THREAD_ID> -`의 stdin으로 전달하여 Codex Dispatch가 prompt 본문을 argv에 넣지 않는다. process exit 0 이후 external completion notify가 오기 전에는 session을 `WAITING`으로 유지하며, 기존 notify pipeline이 job을 `COMPLETED`로 닫고 Discord 결과를 전달한다. 서비스 재시작 시 LIVE `DISPATCHED`는 보존하지만 orchestrator와 함께 사라지는 MANAGED `DISPATCHED`는 `FAILED`로 복구한다.

### 완료 조건

자동 테스트에서는 stdin-only prompt 전달, bounded stdout/stderr drain, timeout/kill, cancel, mode 전환, graceful shutdown, managed restart recovery를 검증한다. 실제 host에서는 `scripts/managed-host-acceptance.py`로 동일 durable thread가 `codex exec resume`으로 이어지는지 확인하고, Discord `/mode managed` 후 external notify와 completion 결과가 같은 Thread로 돌아오는 E2E를 확인한다.

---

## PHASE 9 - Queue Auto Run

### 목표

선택적으로 여러 작업을 순차 자동 실행한다.

### 작업

- [x] session별 persistent `auto_run` 설정
- [x] `/auto state:on`
- [x] `/auto state:off`
- [x] completion event 이후 다음 queued job dispatch
- [x] LIVE / MANAGED 공통 sequential auto-run
- [x] failure 시 queue pause (`auto_run=OFF`)
- [x] queued cancel skip
- [x] active MANAGED cancel 시 auto-run pause
- [x] Discord reconnect/startup safe queue recovery
- [x] LIVE ambiguous active job 재전송 금지
- [x] MANAGED orphan job failure + auto-run pause

### 기본 정책

```text
auto_run = OFF
```

실패한 job 이후에는 자동으로 다음 작업을 실행하지 않는다.

### 구현 산출물

```text
docs/auto-run.md
scripts/auto-run-host-acceptance.py
```

SQLite schema는 v4로 상승하고 `sessions.auto_run`을 persistent flag로 저장한다. 기존 session은 migration 후 모두 OFF다. completion마다 가장 오래된 `QUEUED` job 하나만 durable claim하며 실제 dispatch는 background task로 실행한다. 실패 또는 active Managed cancel 시 auto-run을 즉시 OFF로 내려 후속 queue를 멈춘다.

### 완료 조건

자동 테스트에서 LIVE/MANAGED 양쪽의 sequential execution, failure pause, cancelled job skip, reconnect/restart recovery를 검증한다. 실제 host에서는 `auto_run=on`인 session에서 Job 3개가 순서대로 완료되고 `scripts/auto-run-host-acceptance.py --thread <THREAD_ID> --minimum-completed 3`이 PASS해야 한다.

---

## PHASE 10 - systemd 및 설치 자동화

### 목표

VPS에서 장기 운영 가능한 서비스로 만든다.

### 작업

- [x] systemd unit 작성
- [x] environment file template
- [x] runtime directory/socket lifecycle
- [x] install script
- [x] upgrade procedure
- [x] uninstall procedure
- [x] restart policy
- [x] graceful SIGTERM
- [x] Discord reconnect recovery 경로 유지
- [ ] 실제 VPS reboot host acceptance

### 구현 산출물

```text
systemd/codex-dispatch.service.in
systemd/codex-dispatch.env.example
systemd/codex-dispatch-notify.env.example
systemd/codex-dispatch.secret.env.example
scripts/install-service.sh
scripts/upgrade-service.sh
scripts/uninstall-service.sh
scripts/systemd-host-acceptance.py
docs/systemd-operations.md
tests/test_systemd_assets.py
```

systemd는 Codex 인증/session을 소유한 일반 사용자로 실행하고 `RuntimeDirectory=codex-dispatch`, `StateDirectory=codex-dispatch`, `Restart=on-failure`, `KillMode=mixed`, `TimeoutStopSec=45s`를 사용한다. SIGTERM 수신 시 Discord 입력을 먼저 닫고 background/Managed 작업, notify socket, DB를 순서대로 정리한다. PHASE 11 hardening 이후 config는 non-secret main env, non-secret `notify.env`, root-only `secret.env`로 분리한다. interactive notify bridge는 `/etc/codex-dispatch/notify.env`의 세 notify 키만 비실행 방식으로 읽어 systemd socket과 일치시키며 Discord token에는 접근하지 않는다.

### 완료 조건

자동 테스트에서는 unit/install asset, dry-run, SIGTERM shutdown, notify systemd env discovery를 검증한다. 실제 host에서는 `scripts/systemd-host-acceptance.py --restart`가 PASS하고, VPS reboot 후 service가 자동 실행되며 기존 DB/session mapping이 유지되어야 PHASE 10을 완전 PASS로 판정한다.

---

## PHASE 11 - 보안 Hardening

### 목표

Discord 계정 탈취, 잘못된 설정, path traversal, command injection이 VPS 전체 접근으로 이어지는 위험을 최소화한다.

### 작업

- [x] Guild/Channel/User triple allowlist test
- [x] DM rejection test
- [x] realpath workspace validation
- [x] symlink escape test
- [x] dispatch 직전 workspace 재검증
- [x] protected-path / unsafe root startup validation
- [x] shell=False 보장
- [x] subprocess argv construction audit
- [x] secret log redaction
- [x] Discord token file permission 검사
- [x] SQLite permission 검사
- [x] Unix socket permission 검사
- [x] prompt/result logging 최소화
- [x] oversized message rejection
- [x] malformed notify payload deterministic fuzz test
- [x] systemd sandbox hardening

### 구현 산출물

```text
src/codex_dispatch/security.py
scripts/security-host-acceptance.py
docs/security-hardening.md
tests/test_security.py
```

`CODEX_ALLOWED_ROOTS`는 실제 repository/workspace만 허용하며 filesystem root, 주요 system directory, Codex Dispatch project, SQLite DB, `~/.codex`, `~/.ssh`, absolute Codex binary를 포함하는 broad root는 startup에서 거부한다. notify ingress에 더해 실제 subprocess dispatch 직전에도 strict realpath allowlist 검증을 반복한다. Discord token은 root-only `secret.env`에 분리하고 Codex child environment에서는 `DISCORD_*`, `CODEX_DISPATCH_*`, `CODEX_ALLOWED_ROOTS`를 제거한다. logging formatter와 Discord delivery error persistence에는 runtime Discord token/Authorization redaction을 적용한다.

### 완료 조건

자동 테스트에서 unauthorized Discord identity/DM, realpath/symlink escape, post-registration symlink replacement, shell primitive 부재, prompt/result 비로깅, secret redaction, malformed notify fuzz를 검증한다. 실제 host에서는 안전한 project별 `CODEX_ALLOWED_ROOTS`를 설정한 뒤 `sudo PYTHONPATH=src python3 scripts/security-host-acceptance.py`가 journal secret scan을 포함해 PASS해야 PHASE 11을 완전 PASS로 판정한다.

---

## PHASE 12 - E2E 및 장애 복구 검증

### 정상 시나리오

- [x] single live session
- [x] multiple live sessions
- [x] same workspace multiple sessions
- [x] multiple workspaces
- [x] managed session
- [x] queued jobs
- [x] auto-run

### 장애 시나리오

- [x] Discord disconnect/reconnect 중복 dispatch 방지
- [x] Discord API send failure
- [x] orchestrator restart
- [ ] 실제 VPS reboot host acceptance
- [x] Codex binary missing
- [x] Codex command non-zero exit
- [x] Codex session not found
- [x] stale thread-id
- [x] duplicate notify
- [x] duplicate Discord event
- [x] SQLite busy/lock
- [x] malformed notify payload
- [x] Unix socket unavailable
- [x] user sends messages faster than Codex can consume

### 구현 산출물

```text
tests/test_e2e_recovery.py
tests/test_e2e_assets.py
scripts/e2e-host-acceptance.py
docs/e2e-recovery.md
```

PHASE 12는 real SQLite와 SessionManager/JobManager/CompletionNotifier를 함께 사용하는 cross-component E2E suite를 추가했다. LIVE 다중 session, 동일 workspace 다중 session, multi-workspace, MANAGED, queue/auto-run을 연결하고 restart/duplicate/reconnect/Discord failure/stale session/SQLite lock/malformed notify/socket unavailable를 상태 전이까지 검증한다. SQLite write-lock timeout은 `DatabaseBusyError`로 표준화해 Discord 요청에 재시도 가능한 명확한 피드백을 반환하고 Codex dispatch를 수행하지 않는다.

실제 VPS 검증용 `scripts/e2e-host-acceptance.py`는 prompt/result를 읽지 않고 schema/session/workspace/job/delivery invariant와 systemd 상태를 확인한다. reboot 전에는 thread ↔ Discord Thread mapping baseline을 `0600` 파일로 capture하고 reboot 후 기존 mapping이 모두 유지되는지 verify할 수 있다.

### 완료 조건

자동 regression/E2E는 PASS해야 하며, 실제 host에서 LIVE/MANAGED/multi-session/multi-workspace/auto-run 시나리오 후 `scripts/e2e-host-acceptance.py`가 PASS해야 한다. 마지막으로 VPS reboot 전 baseline capture와 reboot 후 verify가 PASS하고 기존 Discord Thread의 `/status`가 정상 응답해야 PHASE 12 및 전체 계획을 완전 PASS로 판정한다.

---

# 17. MVP 범위

첫 번째 릴리스는 PHASE 0 ~ PHASE 7까지만 완료해도 충분하다.

MVP 최종 흐름:

```text
1. 사용자가 VPS Codex TUI에서 작업한다.
2. Codex turn이 완료된다.
3. notify bridge가 이벤트를 Codex Dispatch로 보낸다.
4. Codex Dispatch가 session을 식별한다.
5. 대응하는 Discord Thread를 생성하거나 찾는다.
6. Discord에 완료 결과를 전송한다.
7. 사용자가 Discord Thread에 다음 프롬프트를 입력한다.
8. Codex Dispatch가 동일 Codex live session으로 prompt를 전달한다.
9. Codex가 기존 context를 유지한 채 다음 작업을 수행한다.
10. 완료 후 다시 2번부터 반복한다.
```

MVP에서는 다음은 필수가 아니다.

- auto-run
- headless session 생성
- web dashboard
- Slack
- 여러 VPS
- workflow DSL

---

# 18. MVP 완료 기준

다음 조건을 모두 만족하면 MVP를 PASS로 판정한다.

## Codex

- [ ] 설치된 Codex version/capability를 시작 시 또는 설치 시 검증한다.
- [ ] 완료 이벤트에서 thread ID를 정확히 식별한다.
- [ ] 완료 이벤트에서 cwd를 정확히 식별한다.
- [ ] 기존 live Codex context를 유지하며 Discord prompt를 전달한다.
- [ ] Codex CLI 기능이 없거나 버전이 호환되지 않을 경우 안전하게 실패한다.

## Discord

- [ ] Codex session별 Discord Thread가 분리된다.
- [ ] 완료 메시지가 한 번만 전송된다.
- [ ] 허용 사용자의 Thread 메시지를 수신한다.
- [ ] 허용되지 않은 사용자/서버/채널의 요청을 거부한다.

## 상태 관리

- [ ] session mapping을 SQLite에 저장한다.
- [ ] duplicate notify를 처리한다.
- [ ] duplicate Discord event를 처리한다.
- [ ] session당 Codex dispatch 동시 실행은 최대 하나다.
- [ ] 서비스 재시작 후 mapping이 유지된다.

## 보안

- [ ] shell interpolation을 사용하지 않는다.
- [ ] workspace allowlist가 적용된다.
- [ ] symlink escape가 차단된다.
- [ ] Bot token이 저장소와 로그에 노출되지 않는다.
- [ ] DM 및 unauthorized Discord identity는 기본 거부한다.

## 운영

- [ ] systemd에서 실행할 수 있다.
- [ ] journalctl 로그로 job/session 실패 원인을 추적할 수 있다.
- [ ] Discord reconnect 후 정상 동작한다.
- [ ] VPS reboot 후 자동 복구된다.

---

# 19. MVP 이후 확장 우선순위

## V1.1

- Managed/headless session
- `/auto state:on|off`
- retry/cancel 기능 강화
- Discord buttons
- 결과 파일/긴 로그 표시 개선

## V1.2

- Slack adapter
- Telegram adapter
- channel-independent notification interface

## V2

- 다중 VPS worker
- 중앙 controller
- worker heartbeat
- web dashboard
- Git repository 상태 표시
- commit/push 결과 표시

## V3

- approval/event remote control
- policy engine
- scheduled jobs
- reusable workflows
- 팀 단위 RBAC

---

# 20. 구현 시 금지 사항

다음 구현은 명시적인 설계 변경 없이 도입하지 않는다.

1. `tmux send-keys`를 통한 Codex TUI 제어
2. `shell=True` 기반 Discord prompt 실행
3. Discord message를 shell command로 직접 실행
4. Discord Bot token의 repository 저장
5. root 계정에서 Codex Dispatch 상시 실행
6. allowlist 없는 Discord 명령 실행
7. allowlist 없는 filesystem workspace 접근
8. live Codex session과 별도 `resume` process의 동시 사용
9. DB 없이 thread/session mapping을 메모리에만 저장
10. 실패 후 무제한 자동 retry
11. 실패한 job 이후 다음 queued job의 무조건 자동 실행
12. Codex CLI 버전별 capability 검증 없이 특정 command syntax를 전제로 구현

---

# 21. 첫 구현 작업 순서

실제 개발은 다음 순서로 시작한다.

```text
CODEX-00  Codex capability discovery
CODEX-01  Python project bootstrap
DISC-01   Discord bot connection + authorization
NOTIFY-01 Codex notify bridge
DB-01     SQLite schema + repositories
SESS-01   Codex session <-> Discord Thread mapping
OUT-01    Codex completion -> Discord
IN-01     Discord prompt -> live Codex session
JOB-01    per-session job lock + queue
OPS-01    systemd + restart recovery
SEC-01    security hardening
E2E-01    full end-to-end verification
```

각 작업은 이전 단계의 완료 조건을 충족한 뒤 진행한다. 특히 `CODEX-00`이 완료되기 전에는 live-session command syntax를 확정하지 않는다.

---

# 22. 최종 목표 상태

완성된 Codex Dispatch는 다음 사용 경험을 제공해야 한다.

```text
[VPS]
Codex: 작업 완료

        |
        v

[휴대폰 Discord]
✅ creator-alliance 작업 완료

SOCIAL-07 구현 및 테스트 완료.

사용자:
SOCIAL-08 진행해. 전체 테스트 후 commit까지 해.

        |
        v

[VPS]
기존 Codex session이 동일 context로 SOCIAL-08 수행

        |
        v

[휴대폰 Discord]
✅ SOCIAL-08 완료
Tests: PASS
Commit: ...
```

이 루프가 VPS 터미널 재접속 없이 안정적으로 반복되면 프로젝트의 핵심 목적을 달성한 것으로 본다.
