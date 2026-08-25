# PHASE 11 - Security Hardening

Codex Dispatch는 Discord 입력을 실제 Codex 실행으로 연결하므로 일반 알림 Bot보다 강한 신뢰 경계가 필요하다. 이 문서는 PHASE 11에서 적용한 방어선과 host acceptance 절차를 설명한다.

## 1. 보안 모델

허용된 Discord 사용자가 보내는 메시지는 최종적으로 Codex 작업이 된다. 따라서 `CODEX_ALLOWED_ROOTS`는 사실상 원격으로 수정 가능한 파일시스템 범위다.

다음과 같은 설정은 금지한다.

```text
CODEX_ALLOWED_ROOTS=/
CODEX_ALLOWED_ROOTS=/etc
CODEX_ALLOWED_ROOTS=/home/<user>
```

홈 전체를 허용하면 `.codex`, `.ssh`, Codex 실행파일, Codex Dispatch 소스/venv가 함께 노출될 수 있다.

대신 실제 원격 제어 대상 repository/workspace만 명시한다.

```text
CODEX_ALLOWED_ROOTS=/home/<user>/creator-alliance:/home/<user>/justice-forum
```

PHASE 11 startup validation은 다음 protected path가 허용 root 아래에 들어가는 배포를 거부한다.

- Codex Dispatch project/source
- SQLite database
- `~/.codex`
- `~/.ssh`
- absolute `CODEX_DISPATCH_CODEX_BIN`
- `/`, `/etc`, `/usr`, `/boot`, `/root`, `/proc`, `/sys`, `/dev`, `/run`

허용 root는 startup 시 실제 존재하는 디렉터리여야 한다.

## 2. Workspace 이중 검증

workspace는 두 번 검증한다.

1. Codex external notify ingress
2. 실제 Codex subprocess 실행 직전

각 검증은 absolute path → strict realpath → existing directory → allowed-root membership 순서로 수행한다.

두 번째 검증은 notify 등록 이후 디렉터리가 symlink로 교체되거나 persistent DB에 오래된 path가 남아 있는 경우를 방어한다.

검증 실패 시 Codex runner는 호출하지 않고 해당 Job은 `FAILED`, session은 `ERROR`가 된다. auto-run도 기존 failure 정책에 따라 OFF로 내려간다.

## 3. Discord trust boundary

Discord ingress는 fail-closed다.

다음 세 allowlist를 모두 통과해야 한다.

```text
DISCORD_ALLOWED_GUILD_IDS
DISCORD_ALLOWED_CHANNEL_IDS
DISCORD_ALLOWED_USER_IDS
```

추가 정책:

- DM 거부
- Bot author 거부
- webhook message 거부
- system message 거부
- 빈 text 거부
- Thread는 parent channel이 허용된 경우만 허용
- outbound message/thread 생성에도 channel authorization 재적용

## 4. Shell / subprocess 정책

Codex Dispatch runtime source에서는 다음 API를 사용하지 않는다.

```text
asyncio.create_subprocess_shell
shell=True
os.system(...)
subprocess.call(...)
subprocess.Popen(...)
```

Codex CLI는 `asyncio.create_subprocess_exec`의 argv로만 실행한다.

LIVE mode는 upstream `codex queue --message` 계약 때문에 prompt가 argv에 들어간다. 따라서 동일 VPS에서 임의 사용자가 다른 process argv를 볼 수 있는 운영 환경은 피해야 한다.

MANAGED mode는 다음처럼 prompt를 stdin으로 전달한다.

```text
codex exec resume <THREAD_ID> -
```

prompt 본문은 Codex Dispatch가 argv에 추가하지 않는다.

## 5. Secret / logging 정책

운영 로그는 prompt/result 본문을 기록하지 않는다. 기록 가능한 데이터는 job/session ID, Discord snowflake, workspace path, prompt 길이와 같은 운영 metadata다.

logging formatter는 다음을 redaction한다.

- 현재 runtime의 `DISCORD_BOT_TOKEN` 실제 값
- `DISCORD_BOT_TOKEN=...`
- `Authorization: Bot ...`
- `Authorization: Bearer ...`

Discord delivery error를 SQLite에 기록할 때도 runtime token을 redaction한다.

Bot token이 과거 로그에 이미 노출된 적이 있다면 redaction 적용만으로 복구되지 않으므로 Discord Developer Portal에서 token을 rotate해야 한다. 또한 PHASE 10의 단일 `codex-dispatch.env`를 `root:<service-group> 0640`으로 실제 배포한 적이 있다면 service user가 token 파일을 읽을 수 있었던 기간이 있었으므로 PHASE 11 업그레이드 후 token rotation을 권장한다.

## 6. 파일 권한

설치 기본 정책:

```text
/etc/codex-dispatch/codex-dispatch.env   root:<service-group> 0640  # non-secret
/etc/codex-dispatch/notify.env           root:<service-group> 0640  # non-secret
/etc/codex-dispatch/secret.env           root:root            0600  # Discord token only
/var/lib/codex-dispatch/...db            <service-user>       0600
/run/codex-dispatch                      <service-user>       0700
/run/codex-dispatch/notify.sock          <service-user>       0600
```

Discord Bot token은 service user가 직접 읽을 수 있는 main/notify config와 분리한다. systemd manager가 root-only `secret.env`를 읽어 서비스에 전달하고, Codex child를 생성할 때는 `DISCORD_*`, `CODEX_DISPATCH_*`, `CODEX_ALLOWED_ROOTS`를 child environment에서 제거한다. `CODEX_HOME`이나 OpenAI 인증 관련 일반 환경은 Codex CLI 동작을 위해 유지한다.

main/notify EnvironmentFile은 other permission과 group write를 허용하지 않는다. `secret.env`는 group/other read도 허용하지 않는다.

## 7. systemd sandbox

PHASE 11 unit에는 다음 hardening을 적용한다.

```ini
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectHostname=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077
```

`ProtectSystem=full`은 `/usr`, `/boot`, `/etc` 등을 read-only로 만들지만 `/home`의 명시적 project workspace 작업은 유지한다.

Codex가 container namespace, device access 등 추가 OS 기능을 필요로 하는 별도 workflow는 PHASE 12 E2E에서 개별 검증해야 한다. 필요하다는 이유만으로 전체 hardening을 제거하지 말고 필요한 최소 directive만 조정한다.

## 8. Size / malformed input 방어

다음 경계가 있다.

- Discord/Codex prompt: `CODEX_DISPATCH_CODEX_PROMPT_MAX_CHARS` 기본 8,000자
- notify frame: `CODEX_DISPATCH_NOTIFY_MAX_BYTES` 기본 262,144 bytes
- notify IDs/client: field별 최대 길이
- notify input message count: 최대 256
- Discord outbound: 2,000자
- Managed stdout/stderr retained output: 각각 bounded capture

테스트 corpus는 malformed notify field type, control character, overlong field, relative/outside cwd, path traversal 형태를 deterministic fuzz 방식으로 반복 검증한다.

## 9. 배포 전 설정 예시

`/etc/codex-dispatch/codex-dispatch.env`에는 non-secret runtime 설정과 실제 project root만 지정한다.

```text
CODEX_ALLOWED_ROOTS=/home/ivenewjeans25/creator-alliance:/home/ivenewjeans25/justice-forum
CODEX_DISPATCH_CODEX_BIN=/home/ivenewjeans25/.npm-global/bin/codex
DISCORD_CONTROL_CHANNEL_ID=<channel-id>
DISCORD_ALLOWED_GUILD_IDS=<guild-id>
DISCORD_ALLOWED_CHANNEL_IDS=<channel-id>
DISCORD_ALLOWED_USER_IDS=<user-id>
```

Discord token은 별도의 `/etc/codex-dispatch/secret.env`에만 둔다.

```text
DISCORD_BOT_TOKEN=<secret>
```

다음처럼 설정하면 PHASE 11 startup validation에서 거부되어야 한다.

```text
CODEX_ALLOWED_ROOTS=/home/ivenewjeans25
```

Codex Dispatch 자체가 `/home/ivenewjeans25/codex-dispatch`에 있기 때문이다.

## 10. Host acceptance

먼저 최신 unit/config를 배포하고 서비스를 재시작한다.

```bash
cd /home/ivenewjeans25/codex-dispatch
sudo bash scripts/upgrade-service.sh
```

main config에서 `CODEX_ALLOWED_ROOTS`를 안전한 project별 경로로 수정하고 root-only secret 파일에 Discord token이 있는지 확인한 뒤:

```bash
sudoedit /etc/codex-dispatch/codex-dispatch.env
sudoedit /etc/codex-dispatch/secret.env
sudo systemctl restart codex-dispatch.service
sudo systemctl status codex-dispatch.service --no-pager
```

보안 acceptance:

```bash
cd /home/ivenewjeans25/codex-dispatch
sudo PYTHONPATH=src python3 scripts/security-host-acceptance.py
```

검사 항목:

- non-root service user
- Discord token 존재 여부(값은 출력하지 않음)
- non-secret main/notify env owner/mode 및 secret env root:root 0600
- SQLite owner/mode
- runtime directory/socket owner/mode
- absolute Codex binary 및 writable-bit 검사
- allowed root와 protected path overlap
- systemd sandbox directive
- source shell primitive audit
- 최근 journal에 실제 Discord token이 없는지 검사

최근 journal을 읽을 권한이 없는 비-root 진단에서만 다음 옵션을 사용할 수 있다.

```bash
PYTHONPATH=src python3 scripts/security-host-acceptance.py --skip-journal
```

최종 PHASE 11 PASS에는 `--skip-journal` 없이 검사하는 것을 권장한다.

## 11. 남는 신뢰 경계

PHASE 11은 Discord 입력을 일반 텍스트로 안전하게 전달하고 실행 범위를 제한하지만, 허용된 workspace 안에서 Codex가 수행하는 작업 자체를 의미적으로 제한하지는 않는다.

또한 Bot token 파일과 child environment를 분리했더라도 orchestrator parent process 자체에는 Discord token이 필요하다. Codex child가 동일 Linux UID로 실행되므로, 호스트의 ptrace/proc 보안 정책이 느슨한 환경에서는 같은 UID process 간 inspection이 추가 신뢰 경계가 될 수 있다. 완전한 credential process isolation은 별도 runner UID/broker 구조가 필요한 더 큰 아키텍처 변경이다.

즉 허용 Discord 계정이 탈취되면 공격자는 허용 workspace 내부에서는 Codex 권한 범위의 변경을 요청할 수 있다. 따라서 다음 운영 정책도 중요하다.

- Discord user allowlist를 최소화한다.
- Bot은 전용 server/channel에서 사용한다.
- repository별 최소 `CODEX_ALLOWED_ROOTS`를 유지한다.
- 개인 SSH key, Codex auth, service source를 workspace 안에 두지 않는다.
- 중요한 repository는 Git remote/branch protection과 별도 backup을 사용한다.
