# PHASE 10 - systemd deployment and operations

Codex Dispatch는 항상 **Codex 인증/session을 실제로 소유한 일반 Linux 사용자**로 실행한다. root service로 실행하지 않는다.

운영 기본 경로:

```text
service unit : /etc/systemd/system/codex-dispatch.service
config       : /etc/codex-dispatch/codex-dispatch.env
notify config: /etc/codex-dispatch/notify.env
secret       : /etc/codex-dispatch/secret.env
install meta : /etc/codex-dispatch/install.conf
runtime      : /run/codex-dispatch
notify socket: /run/codex-dispatch/notify.sock
state        : /var/lib/codex-dispatch
SQLite       : /var/lib/codex-dispatch/codex-dispatch.db
```

systemd가 `RuntimeDirectory=`와 `StateDirectory=`를 사용해 runtime/state 디렉터리를 service user 소유로 생성한다.

---

## 1. 설치 전 확인

Codex를 평소 실행하는 사용자에서 다음을 확인한다.

```bash
whoami
command -v codex
codex --version
python3 --version
```

서비스 사용자는 root가 아니어야 한다.

프로젝트에서 먼저 dry-run을 실행한다.

```bash
cd /home/<codex-user>/codex-dispatch
bash scripts/install-service.sh --dry-run --user <codex-user>
```

Codex가 자동 탐지되지 않으면 절대경로를 지정한다.

```bash
bash scripts/install-service.sh \
  --dry-run \
  --user <codex-user> \
  --codex-bin /home/<codex-user>/.npm-global/bin/codex
```

---

## 2. 설치

일반적인 sudo 사용에서는 `SUDO_USER`를 통해 service user를 자동으로 선택한다.

```bash
cd /home/<codex-user>/codex-dispatch
sudo bash scripts/install-service.sh
```

필요하면 명시한다.

```bash
sudo bash scripts/install-service.sh \
  --user <codex-user> \
  --codex-bin /home/<codex-user>/.npm-global/bin/codex
```

설치 스크립트가 수행하는 작업:

1. root runtime을 거부하고 실제 service user를 결정한다.
2. Codex 절대경로를 결정한다.
3. 프로젝트 `.venv`를 service user 권한으로 생성한다.
4. `pip install -e`로 runtime dependency를 설치한다.
5. non-secret main config와 notify config, root-only secret config를 생성한다.
6. 기존 config는 덮어쓰지 않고 권한을 정상화한다.
7. 이전 PHASE 10 main env에 `DISCORD_BOT_TOKEN`이 있으면 값을 출력하지 않고 `secret.env`로 이전한 뒤 main env에서 제거한다.
8. systemd unit을 실제 사용자/프로젝트 경로로 렌더링한다.
9. `systemctl daemon-reload` 후 service를 enable한다.

설치 기본 권한:

```text
/etc/codex-dispatch/codex-dispatch.env  root:<service-group> 0640  # non-secret
/etc/codex-dispatch/notify.env          root:<service-group> 0640  # non-secret
/etc/codex-dispatch/secret.env          root:root            0600  # Discord token only
```

Bot token은 repository, systemd unit, service-user-readable config에 저장하지 않는다.

---

## 3. 환경 설정

설치 후 non-secret 설정과 secret을 분리해 채운다. 프로젝트의 local `.env`에 이미 `CODEX_ALLOWED_ROOTS`와 Discord 설정을 준비했다면, allowlist parser를 사용해 필요한 키만 안전하게 가져올 수 있다. 이 helper는 `.env`를 `source`하거나 실행하지 않고 Bot token을 출력하지 않는다.

```bash
cd /home/<codex-user>/codex-dispatch
sudo bash scripts/configure-service-from-env.sh \
  --user <codex-user> \
  --env-file .env
```

수동 편집도 가능하다.

```bash
sudoedit /etc/codex-dispatch/codex-dispatch.env
sudoedit /etc/codex-dispatch/secret.env
```

main config 예:

```text
CODEX_DISPATCH_DB_PATH=/var/lib/codex-dispatch/codex-dispatch.db
CODEX_ALLOWED_ROOTS=/home/<codex-user>/project-a:/home/<codex-user>/project-b
CODEX_DISPATCH_CODEX_BIN=/home/<codex-user>/.npm-global/bin/codex

DISCORD_CONTROL_CHANNEL_ID=<channel-id>
DISCORD_ALLOWED_GUILD_IDS=<guild-id>
DISCORD_ALLOWED_CHANNEL_IDS=<channel-id>
DISCORD_ALLOWED_USER_IDS=<user-id>
```

root-only secret config:

```text
DISCORD_BOT_TOKEN=<secret>
```

`/etc/codex-dispatch/notify.env`는 기본적으로 설치값을 그대로 사용한다.

`CODEX_ALLOWED_ROOTS`는 가능한 좁게 지정한다. 홈 디렉터리 전체, Codex Dispatch 자체, `.codex`, `.ssh`, Codex binary 또는 SQLite DB를 포함하는 broad root는 PHASE 11 startup security validation에서 거부된다.

---

## 4. Codex external notify 연결

Codex CLI의 `notify`는 기존 source checkout wrapper를 사용할 수 있다.

예:

```toml
notify = [
    "/home/<codex-user>/codex-dispatch/.venv/bin/python",
    "/home/<codex-user>/codex-dispatch/bin/codex-notify.py"
]
```

notify client는 `CODEX_DISPATCH_NOTIFY_SOCKET`가 현재 shell environment에 없으면 비밀이 없는 `/etc/codex-dispatch/notify.env`에서 **notify 관련 키만 안전하게 읽는다.** 파일을 shell로 source하거나 실행하지 않는다.

읽는 키는 다음 세 개뿐이다.

```text
CODEX_DISPATCH_NOTIFY_SOCKET
CODEX_DISPATCH_NOTIFY_MAX_BYTES
CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS
```

따라서 interactive Codex TUI와 systemd service가 같은 `/run/codex-dispatch/notify.sock`을 사용한다.

---

## 5. 서비스 시작

환경 설정을 완료한 뒤:

```bash
sudo systemctl start codex-dispatch.service
sudo systemctl status codex-dispatch.service --no-pager
```

설치와 동시에 시작하려면 이미 완성된 env file이 있는 상태에서:

```bash
sudo bash scripts/install-service.sh --start
```

를 사용할 수 있다. `CODEX_ALLOWED_ROOTS` 또는 필수 Discord 값이 비어 있으면 `--start`는 시작을 거부한다.

로그:

```bash
sudo journalctl -u codex-dispatch.service -f
```

최근 부팅 로그:

```bash
sudo journalctl -u codex-dispatch.service -b --no-pager
```

---

## 6. systemd lifecycle 정책

unit의 핵심 정책:

```text
User=<codex-user>
Restart=always
RestartSec=5s
KillSignal=SIGTERM
KillMode=mixed
TimeoutStopSec=45s
RuntimeDirectory=codex-dispatch
StateDirectory=codex-dispatch
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectHostname=true
RestrictSUIDSGID=true
LockPersonality=true
WantedBy=multi-user.target
```

`KillMode=mixed`는 정상 종료 시 SIGTERM을 main orchestrator에 먼저 전달한다. Codex Dispatch는:

1. Discord Gateway 입력을 중단한다.
2. background auto-dispatch를 취소한다.
3. Managed child process를 terminate/kill하고 상태를 DB에 반영한다.
4. notify socket을 닫는다.
5. SQLite를 닫는다.

`TimeoutStopSec` 안에 끝나지 않는 잔여 process는 systemd가 정리한다.

`Restart=always`는 예상치 못한 SIGTERM/SIGINT나 정상 exit가 service를 장시간 내려둔 채 남기지 않도록 한다. 관리자가 명시적으로 `systemctl stop codex-dispatch.service`를 실행한 경우에는 systemd가 의도된 stop으로 처리하므로 자동 재시작하지 않는다.

---

## 7. Restart acceptance

최소 하나의 실제 session mapping이 생성된 뒤 다음을 실행한다.

```bash
cd /home/<codex-user>/codex-dispatch
sudo PYTHONPATH=src python3 scripts/systemd-host-acceptance.py \
  --restart \
  --minimum-sessions 1
```

검증 항목:

- service enabled
- service active
- root가 아닌 service user
- `Restart=always`
- `KillMode=mixed`
- environment file 권한
- runtime directory `0700`
- notify socket `0600`
- SQLite `0600`
- schema version 유지
- restart 전후 Codex thread ↔ Discord Thread mapping 유지

검증 도구는 SQLite를 read-only URI로 열어 root 소유 WAL/SHM 파일을 만들지 않는다.

---

## 8. 실제 reboot acceptance

PHASE 10의 최종 host acceptance에는 실제 VPS reboot가 포함된다.

먼저:

```bash
sudo systemctl is-enabled codex-dispatch.service
sudo systemctl is-active codex-dispatch.service
```

그 다음 VPS를 재부팅한다.

```bash
sudo reboot
```

재접속 후:

```bash
systemctl is-active codex-dispatch.service
cd /home/<codex-user>/codex-dispatch
PYTHONPATH=src python3 scripts/systemd-host-acceptance.py --minimum-sessions 1
```

마지막으로 기존 Discord Thread에서 `/status`를 실행해 재부팅 전 session mapping이 유지되는지 확인한다.

---

## 9. 업그레이드

Git checkout을 원하는 commit으로 업데이트한 뒤:

```bash
cd /home/<codex-user>/codex-dispatch
sudo bash scripts/upgrade-service.sh
```

upgrade script는 설치 시 기록한 `/etc/codex-dispatch/install.conf`를 사용한다.

서비스가 실행 중이었다면:

1. stop
2. unit/runtime package 재설치
3. daemon-reload
4. start

순서로 진행한다.

기존 `/etc/codex-dispatch/codex-dispatch.env`와 SQLite state는 보존된다. 재설치 단계가 실패하더라도 업그레이드 시작 전에 service가 active였다면 upgrade script가 service 재시작을 시도해 장시간 stopped 상태로 남는 것을 방지한다.

업그레이드 후:

```bash
sudo systemctl status codex-dispatch.service --no-pager
sudo journalctl -u codex-dispatch.service -n 100 --no-pager
```

을 확인한다.

---

## 10. 제거

기본 제거는 unit만 제거하고 설정과 SQLite를 보존한다.

```bash
sudo bash scripts/uninstall-service.sh
```

설정 제거:

```bash
sudo bash scripts/uninstall-service.sh --purge-config
```

SQLite state 제거:

```bash
sudo bash scripts/uninstall-service.sh --purge-state
```

모든 운영 데이터와 `.venv`까지 제거:

```bash
sudo bash scripts/uninstall-service.sh --purge
```

`--purge-state`는 session mapping/job 기록을 영구 삭제하므로 필요한 경우 DB 백업 후 실행한다.

---

## 11. 장애 확인

서비스 시작 실패:

```bash
sudo systemctl status codex-dispatch.service --no-pager
sudo journalctl -u codex-dispatch.service -n 200 --no-pager
```

unit 확인:

```bash
sudo systemctl cat codex-dispatch.service
sudo systemctl show codex-dispatch.service \
  -p User -p Group -p Restart -p KillMode -p RuntimeDirectory -p StateDirectory
```

notify socket:

```bash
ls -ld /run/codex-dispatch
ls -l /run/codex-dispatch/notify.sock
```

persistent state:

```bash
ls -ld /var/lib/codex-dispatch
ls -l /var/lib/codex-dispatch/codex-dispatch.db
```
