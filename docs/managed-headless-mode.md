# PHASE 8 - Managed / Headless Mode

## 1. 목적

PHASE 8은 interactive Codex TUI가 없어도 기존 Codex thread context를 이어서 실행할 수 있는 Managed 모드를 제공한다.

LIVE와 MANAGED의 차이는 다음과 같다.

```text
LIVE
Discord -> codex queue --thread <THREAD_ID> --message <PROMPT>
         -> 이미 실행 중인 interactive Codex session

MANAGED
Discord -> codex exec resume <THREAD_ID> -
         -> prompt는 stdin으로 전달
         -> Codex Dispatch가 subprocess lifecycle을 직접 관리
```

한 session은 한 시점에 정확히 하나의 mode만 가진다.

---

## 2. Managed command contract

Managed runner는 다음 command shape을 사용한다.

```text
codex exec resume <THREAD_ID> -
```

`-`는 prompt를 stdin에서 읽도록 하기 위해 사용한다.

Codex Dispatch는 prompt text를 process argv에 넣지 않는다.

개념적으로:

```text
stdin: <PROMPT>
argv : codex, exec, resume, <THREAD_ID>, -
```

이 방식은 LIVE mode의 `--message <PROMPT>`보다 local process listing에 prompt가 노출될 위험을 줄인다.

---

## 3. Capability verification

서비스 시작 시 두 capability를 모두 확인한다.

```text
codex queue --help
codex exec resume --help
```

Managed capability는 help command가 성공하고 resume target이 session/thread 개념을 제공하는지 검사한다.

현재 설치된 Codex CLI가 이를 지원하지 않으면 서비스는 fail-fast 한다.

실제 VPS에 설치된 CLI의 help/output이 최종 contract다.

---

## 4. Mode 전환

Discord session Thread에서:

```text
/mode mode:managed
/mode mode:live
```

를 사용한다.

Mode 전환은 다음 조건에서만 허용한다.

- session status가 `RUNNING` 또는 `WAITING`이 아님
- `QUEUED` job 없음
- `DISPATCHED` job 없음

따라서 실행 중인 job의 runner 종류가 중간에 바뀔 수 없다.

MANAGED로 변경한 뒤에는 동일 Codex thread를 interactive TUI에서 동시에 조작하지 않는 것을 운영 규칙으로 한다. Codex Dispatch 내부에서는 동일 thread에 LIVE queue와 managed resume을 동시에 실행하지 않는다.

---

## 5. Managed job lifecycle

```text
Discord message
   |
   v
Job QUEUED
   |
   v
DISPATCHED + session RUNNING
   |
   v
codex exec resume <thread> -
   |
   +-- error ----------> FAILED
   |
   +-- cancelled ------> CANCELLED
   |
   +-- timeout --------> FAILED
   |
   +-- exit 0
          |
          v
      session WAITING
          |
          v
    external notify
          |
          v
      COMPLETED
      session IDLE
```

Managed subprocess exit code 0 자체만으로 Codex turn completion message를 새로 합성하지 않는다.

결과 전달은 기존 Codex external `agent-turn-complete` notify를 계속 사용한다. 이렇게 하면 LIVE와 MANAGED 모두 동일한 completion pipeline을 사용한다.

---

## 6. stdout / stderr 정책

Managed subprocess stdout/stderr는 deadlock을 방지하기 위해 끝까지 drain한다.

단, 메모리에 보관하는 크기는 각각 다음 설정으로 제한한다.

```text
CODEX_DISPATCH_MANAGED_OUTPUT_MAX_BYTES=262144
```

기본값은 stream당 256 KiB다.

limit를 넘은 데이터는 계속 읽어서 pipe를 비우지만 메모리에 추가 저장하지 않는다.

prompt/result 전체를 INFO log에 기록하지 않는다.

---

## 7. Timeout

기본 managed process wall-clock timeout:

```text
CODEX_DISPATCH_MANAGED_TIMEOUT_SECONDS=21600
```

기본값은 6시간이다.

시간을 초과하면 child process를 kill하고 해당 job은 `FAILED`, session은 `ERROR`가 된다.

---

## 8. Process tracking

`CodexManagedRunner`는 실행 중인 process를 `thread-id -> Process`로 추적한다.

같은 thread로 두 번째 managed process를 시작하려 하면 거부한다.

또한 JobManager와 SQLite는 session당 `DISPATCHED` job을 하나만 허용하므로 application layer와 database layer 양쪽에서 중복 실행을 차단한다.

---

## 9. Cancellation

QUEUED job은 기존과 같이:

```text
/cancel job_id:<ID>
```

로 DB에서 취소한다.

MANAGED mode의 현재 `DISPATCHED` job도 같은 명령으로 취소할 수 있다.

Managed active cancellation flow:

```text
/cancel
   |
   v
managed process terminate
   |
   +-- 5초 안에 종료되지 않음 -> kill
   |
   v
job CANCELLED
session IDLE
```

LIVE mode에서 이미 `codex queue`로 전달된 `DISPATCHED` job은 외부 side effect가 발생했으므로 이 단계에서는 `/cancel`로 강제 취소하지 않는다.

---

## 10. Graceful shutdown

서비스가 정상 종료될 때 active managed process는 terminate 후 필요하면 kill한다.

종료 시 아직 `DISPATCHED` 상태인 managed job은:

```text
FAILED
error = service shutdown during managed execution
session = ERROR
```

로 저장한다.

프로세스가 사라졌는데 DB만 RUNNING으로 남는 상태를 만들지 않는다.

---

## 11. Crash / restart recovery

서비스 crash는 graceful shutdown handler가 실행되지 않을 수 있다.

재시작 시 PHASE 7 recovery는 mode에 따라 다르게 처리한다.

### LIVE

기존 interactive session이 여전히 살아 있을 수 있으므로:

```text
DISPATCHED 유지
session RUNNING
```

으로 복구하고 자동 resend하지 않는다.

### MANAGED

managed child process는 orchestrator와 함께 사라졌다고 간주한다.

따라서 orphan `DISPATCHED` job은:

```text
FAILED
error = orchestrator restarted during managed execution
session ERROR
```

로 전환한다.

자동 resume/retry는 하지 않는다.

---

## 12. Host Acceptance

먼저 PHASE 0 capability를 다시 확인한다.

```bash
cd /home/your-user/codex-dispatch
bash scripts/codex-capability-discovery.sh
```

그 다음 기존 durable Codex thread 하나를 대상으로:

```bash
set -a
source /etc/codex-dispatch/codex-dispatch.env
set +a

PYTHONPATH=src python3 scripts/managed-host-acceptance.py \
  --thread <CODEX_THREAD_ID> \
  --cwd /absolute/workspace/path
```

probe prompt는 파일을 수정하지 않고 정확히 다음 문자열만 답하도록 요청한다.

```text
CODEX_DISPATCH_PHASE8_OK
```

PASS는 command exit 0만으로 판정하지 않는다.

다음 모두 확인한다.

1. `codex exec resume` capability PASS
2. 같은 thread context가 resume됨
3. subprocess exit 0
4. actual Codex external notify 발생
5. Discord mapped Thread에 completion message 도착
6. DB active job이 `COMPLETED`로 종료

---

## 13. Discord E2E

실제 사용 검증:

```text
1. Codex session Discord Thread에서 /mode managed
2. 일반 메시지로 파일을 수정하지 않는 probe prompt 입력
3. /status에서 Mode: MANAGED 확인
4. 실행 중 /status에서 RUNNING 확인
5. process 종료 직후 notify 전이면 WAITING 가능
6. completion notify 후 IDLE / COMPLETED 확인
7. 같은 thread의 기존 context가 유지됐는지 결과로 확인
```

그 뒤:

```text
/mode live
```

로 되돌리고 LIVE queue mode도 다시 정상인지 확인한다.

---

## 14. PHASE 8 PASS 기준

- `codex exec resume` startup capability verification
- prompt stdin 전달
- prompt argv 미포함
- LIVE / MANAGED mode persistent 분리
- busy session mode 변경 거부
- 같은 session active execution 최대 1
- stdout/stderr bounded capture + full drain
- managed timeout + process kill
- active managed `/cancel`
- graceful shutdown process cleanup
- managed crash recovery -> FAILED
- actual managed turn에서 external notify 수신
- 동일 Discord Thread로 completion 결과 전달
- 기존 Codex context 유지 확인
