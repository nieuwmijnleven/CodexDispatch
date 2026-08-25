# PHASE 12 - E2E and failure recovery

PHASE 12는 PHASE 0~11의 개별 기능을 실제 운영 흐름으로 연결해 정상/장애 시나리오를 검증한다. 목표는 단순히 오류가 발생하지 않는 것이 아니라, **오류가 중복 Codex 실행이나 orchestrator crash로 번지지 않고 persistent state와 사용자 피드백으로 수렴하는 것**이다.

## 1. 자동 E2E 범위

`tests/test_e2e_recovery.py`는 real SQLite와 SessionManager, JobManager, CompletionNotifier를 함께 사용하고 Discord/Codex 외부 I/O만 deterministic fake로 대체한다.

정상 경로:

- single LIVE session
- multiple LIVE sessions
- 같은 workspace의 여러 Codex thread
- 여러 workspace
- MANAGED resume
- persistent queued jobs
- auto-run sequential execution
- 빠른 동시 Discord 입력에서 session당 active job 1개 유지

장애/복구 경로:

- Discord completion API transient failure와 retry
- orchestrator DB reopen/restart
- restart 중 LIVE `DISPATCHED` job 재전송 금지
- Discord reconnect 시 auto-run 중복 schedule 방지
- stale/missing Codex session → `OFFLINE`
- Codex command rejection → `FAILED` + session `ERROR`
- duplicate Discord message ID
- duplicate notify event
- SQLite write lock → `DatabaseBusyError` + 사용자 재시도 안내
- malformed/path-traversal notify
- unavailable Unix notify socket

기존 unit/security suites가 추가로 고정하는 항목:

- Codex binary missing
- Codex command non-zero exit/timeout
- Discord Gateway reconnect behavior
- unauthorized Guild/Channel/User 및 DM
- oversized prompt/frame
- symlink escape / post-registration workspace replacement
- shell primitive 부재
- systemd SIGTERM/restart policy

## 2. SQLite busy 정책

PHASE 12에서 SQLite lock 오류를 명시적인 `DatabaseBusyError`로 표준화했다.

write transaction이 busy timeout 안에 lock을 획득하지 못하면 raw `sqlite3.OperationalError`를 event handler 밖으로 흘리지 않는다. Discord prompt 경로는 다음처럼 사용자에게 재시도 가능한 상태를 반환한다.

```text
⚠️ 상태 데이터베이스가 일시적으로 사용 중입니다. 잠시 후 같은 요청을 다시 보내 주세요.
```

이 경우:

- 새 Job을 반쯤 생성하지 않는다.
- Codex subprocess를 호출하지 않는다.
- service process를 종료하지 않는다.
- 같은 Discord message를 나중에 다시 접수할 수 있다.

## 3. 실제 VPS E2E 준비

PHASE 11 hardening까지 배포되어 있어야 한다.

```bash
cd /home/<codex-user>/codex-dispatch
sudo bash scripts/upgrade-service.sh
sudo systemctl status codex-dispatch.service --no-pager
```

`CODEX_ALLOWED_ROOTS`에는 최소 두 개의 실제 test workspace를 포함하는 것을 권장한다.

예:

```text
CODEX_ALLOWED_ROOTS=/home/<codex-user>/project-a:/home/<codex-user>/project-b
```

홈 전체나 Codex Dispatch 자체를 포함하는 broad root는 사용하지 않는다.

## 4. 정상 시나리오 Host Acceptance

권장 구성은 최소 세 session이다.

1. `project-a`에서 LIVE session A1 실행 → 한 turn 완료
2. `project-a`에서 별도 session A2 실행 → 한 turn 완료
3. `project-b`에서 LIVE session B1 실행 → 한 turn 완료
4. A2 Discord Thread에서 `/mode mode:managed`로 MANAGED 전환

확인 사항:

- A1/A2/B1이 서로 다른 Discord Thread를 가진다.
- A1/A2는 같은 workspace지만 서로 다른 Codex thread/session으로 유지된다.
- B1은 다른 workspace record에 연결된다.

각 LIVE Thread에서 새 prompt를 한 번씩 보내고 completion이 같은 Discord Thread로 돌아오는지 확인한다.

MANAGED Thread에서는 다음을 수행한다.

```text
/auto state:on
```

첫 Job이 실행 중이거나 completion을 기다리는 동안 후속 prompt 두 개를 빠르게 보낸다. 결과적으로 세 Job이 순서대로 완료되어야 하며 동시에 `DISPATCHED` 상태인 Job은 session당 하나를 넘지 않아야 한다.

## 5. 안전한 장애 시나리오 Host Acceptance

### stale LIVE session

테스트용 LIVE Codex TUI를 종료한 뒤 해당 Discord Thread에서 prompt를 보낸다.

기대 결과:

```text
Job → FAILED
session → OFFLINE
Discord → live 세션을 찾을 수 없다는 안내
```

다른 session과 service process는 계속 동작해야 한다.

### orchestrator restart

LIVE Job이 이미 `DISPATCHED` 상태인 동안:

```bash
sudo systemctl restart codex-dispatch.service
```

restart 후 같은 Job을 자동으로 다시 `codex queue`하지 않아야 한다. 기존 Codex completion notify가 도착하면 그 Job만 `COMPLETED`로 닫힌다.

### Discord reconnect

Discord Gateway 연결이 일시적으로 끊겼다가 복구되어 `on_ready`가 다시 호출돼도 동일 queued Job이 두 번 dispatch되지 않아야 한다. DB claim과 session lock이 최종 방어선이다.

네트워크 차단을 production VPS에 인위적으로 적용할 필요는 없다. 이 경로는 자동 E2E와 Discord adapter reconnect unit test로 검증한다.

### destructive failure probes

Codex binary 삭제, production SQLite 강제 lock, production notify socket 삭제 같은 검증은 운영 host에 직접 수행하지 않는다. 이 항목은 isolated automated test에서 검증한다.

## 6. PHASE 12 상태 Acceptance

정상/장애 시나리오를 수행한 뒤:

```bash
cd /home/<codex-user>/codex-dispatch
PYTHONPATH=src python3 scripts/e2e-host-acceptance.py \
  --minimum-sessions 3 \
  --minimum-workspaces 2 \
  --minimum-live 1 \
  --minimum-managed 1 \
  --minimum-completed-jobs 3 \
  --minimum-sent-deliveries 3 \
  --require-auto-run
```

검사는 prompt/result 본문을 읽지 않는다. 다음 persistent invariant를 확인한다.

- schema v4+
- session 수 및 workspace 수
- LIVE/MANAGED coverage
- 모든 session의 Discord Thread mapping
- completed Job과 SENT delivery 존재
- session당 `DISPATCHED` 최대 1개
- Discord Thread mapping unique
- Discord message ID unique
- notify `(thread_id, turn_id, event_type)` unique
- systemd enabled/active

`mapping_sha256`는 thread ↔ Discord Thread mapping만 canonical JSON으로 hash한 값이며 prompt/result는 포함하지 않는다.

## 7. VPS reboot Acceptance

PHASE 10에서 남아 있던 실제 reboot 검증도 PHASE 12에서 함께 닫는다.

재부팅 직전 mapping baseline을 저장한다.

```bash
sudo PYTHONPATH=src python3 scripts/e2e-host-acceptance.py \
  --minimum-sessions 3 \
  --minimum-workspaces 2 \
  --minimum-live 1 \
  --minimum-managed 1 \
  --minimum-completed-jobs 3 \
  --minimum-sent-deliveries 3 \
  --require-auto-run \
  --capture-baseline /var/lib/codex-dispatch/e2e-reboot-baseline.json
```

baseline에는 schema version과 Codex thread ID ↔ Discord Thread ID mapping만 저장한다. file mode은 `0600`으로 만든다.

그 다음 실제 reboot:

```bash
sudo reboot
```

재접속 후:

```bash
systemctl is-active codex-dispatch.service

sudo PYTHONPATH=src python3 scripts/e2e-host-acceptance.py \
  --minimum-sessions 3 \
  --minimum-workspaces 2 \
  --minimum-live 1 \
  --minimum-managed 1 \
  --minimum-completed-jobs 3 \
  --minimum-sent-deliveries 3 \
  --require-auto-run \
  --verify-baseline /var/lib/codex-dispatch/e2e-reboot-baseline.json
```

기존 baseline에 있던 모든 mapping이 동일하면 다음이 포함되어야 한다.

```text
reboot_baseline=PASS
systemd_enabled=PASS
systemd_active=PASS
Codex Dispatch PHASE 12 acceptance: PASS
```

재부팅 이후 새 session이 추가된 것은 허용하지만 baseline에 있던 기존 session mapping이 변경되거나 사라지는 것은 FAIL이다.

## 8. PHASE 12 최종 PASS 기준

자동 검증:

```bash
bash scripts/test.sh
```

전체 regression과 `tests/test_e2e_recovery.py`가 PASS해야 한다.

실제 host 검증:

1. LIVE/MANAGED/multi-session/multi-workspace 시나리오 수행
2. queued/auto-run 순차 완료 확인
3. stale LIVE session 실패가 다른 session에 영향 없음 확인
4. `scripts/e2e-host-acceptance.py` PASS
5. reboot baseline capture
6. VPS reboot
7. baseline verify PASS
8. 기존 Discord Thread에서 `/status` 정상 응답

위 조건을 모두 만족하면 PHASE 12와 전체 구현 계획을 완전 PASS로 판정한다.
