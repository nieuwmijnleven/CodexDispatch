# CodexDispatch

Linux VPS의 Codex CLI 세션을 Discord에서 원격으로 확인하고 제어하기 위한 오케스트레이터 프로젝트입니다.

## Current implementation

PHASE 0 capability discovery부터 PHASE 12 E2E/장애 복구 검증까지 구현되어 있습니다. 자동 regression/E2E는 로컬에서 검증할 수 있고, 실제 Discord Gateway/Codex/systemd 및 VPS reboot host acceptance는 운영 VPS에서 수행하면 됩니다.

Codex capability 확인:

```bash
bash scripts/codex-capability-discovery.sh
```

테스트 실행:

```bash
bash scripts/test.sh
```

`pytest`가 설치되어 있으면 pytest를 사용하고, 없으면 표준 라이브러리 `unittest`로 동일 테스트를 실행합니다. 개발 의존성은 `pyproject.toml`의 `dev` extra에 선언되어 있습니다.

Discord Bot 설정 및 PHASE 2 host acceptance:

```bash
.venv/bin/python scripts/discord-host-acceptance.py
```

자세한 Discord 설정 절차는 `docs/discord-setup.md`를 참고하세요.

Codex notify bridge host acceptance:

```bash
PYTHONPATH=src python3 scripts/notify-host-acceptance.py
```

notify socket, Codex `notify` 설정, payload 검증/보안 정책은 `docs/notify-bridge.md`를 참고하세요.

PHASE 4 persistent session mapping 확인:

```bash
PYTHONPATH=src python3 scripts/session-host-acceptance.py --minimum-sessions 3
```

SQLite schema, transaction 경계, Codex session ↔ Discord Thread 매핑 정책은 `docs/database-session-manager.md`를 참고하세요.

PHASE 5 completion delivery 확인:

```bash
PYTHONPATH=src python3 scripts/completion-host-acceptance.py --minimum-sent 1
```

완료 메시지 포맷, retry/outbox 정책과 schema v2 delivery state는 `docs/completion-notifications.md`를 참고하세요.

PHASE 6 live-session queue 확인:

```bash
PYTHONPATH=src python3 scripts/live-dispatch-host-acceptance.py --thread <CODEX_THREAD_ID>
```

Discord Thread → Codex session lookup, `codex queue` 실행, timeout/error/status 정책은 `docs/live-codex-dispatch.md`를 참고하세요.

PHASE 7 job queue 및 session serialization 확인:

```bash
PYTHONPATH=src python3 scripts/job-host-acceptance.py --minimum-jobs 3 --minimum-queued 1
```

`/status`, `/queue`, `/cancel`, persistent jobs, Discord message idempotency와 restart recovery 정책은 `docs/job-queue.md`를 참고하세요.

PHASE 8 managed/headless resume 확인:

```bash
PYTHONPATH=src python3 scripts/managed-host-acceptance.py \
  --thread <CODEX_THREAD_ID> \
  --cwd /absolute/workspace/path
```

`/mode live|managed`, stdin prompt 전달, managed process cancellation, bounded output, graceful shutdown 및 managed restart recovery 정책은 `docs/managed-headless-mode.md`를 참고하세요.

PHASE 9 queue auto-run 확인:

```bash
PYTHONPATH=src python3 scripts/auto-run-host-acceptance.py \
  --thread <CODEX_THREAD_ID> \
  --minimum-completed 3
```

`/auto state:on|off`, schema v4 `sessions.auto_run`, failure pause와 safe restart recovery 정책은 `docs/auto-run.md`를 참고하세요.

PHASE 10 systemd 설치 및 restart/reboot acceptance:

```bash
bash scripts/install-service.sh --dry-run --user <codex-user>
sudo bash scripts/install-service.sh
sudo PYTHONPATH=src python3 scripts/systemd-host-acceptance.py --restart --minimum-sessions 1
```

설치, EnvironmentFile, notify socket, upgrade/uninstall, journalctl 및 reboot 검증 절차는 `docs/systemd-operations.md`를 참고하세요.

PHASE 11 security hardening acceptance:

```bash
sudo PYTHONPATH=src python3 scripts/security-host-acceptance.py
```

project별 최소 workspace root, protected-path 차단, secret/log redaction, permission/systemd sandbox 정책은 `docs/security-hardening.md`를 참고하세요.

PHASE 12 E2E 및 reboot mapping acceptance:

```bash
PYTHONPATH=src python3 scripts/e2e-host-acceptance.py \
  --minimum-sessions 3 \
  --minimum-workspaces 2 \
  --minimum-live 1 \
  --minimum-managed 1 \
  --minimum-completed-jobs 3 \
  --minimum-sent-deliveries 3 \
  --require-auto-run
```

LIVE/MANAGED/multi-session/multi-workspace/auto-run 정상 경로와 restart, stale session, duplicate, SQLite lock 등의 장애 복구 및 reboot baseline 절차는 `docs/e2e-recovery.md`를 참고하세요.

실제 Codex 사용자 환경의 capability acceptance 절차와 현재 upstream 기준선은 `docs/codex-capabilities.md`를 참고하세요.

전체 구현 계획은 `docs/implementation-plan.md`에 있습니다.
