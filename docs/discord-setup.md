# Discord Bot Setup and PHASE 2 Acceptance

## 1. 목적

이 문서는 Codex Dispatch의 Discord Bot을 생성하고 PHASE 2 Gateway adapter를 실제 VPS에서 검증하는 절차를 정의한다.

PHASE 2에서 필요한 Discord 기능은 다음뿐이다.

- 지정 Guild의 메시지 이벤트 수신
- 지정 Channel 및 그 하위 Thread의 일반 텍스트 메시지 내용 수신
- 지정 Channel/Thread로 텍스트 메시지 전송

멤버 목록, presence, DM 제어 등은 사용하지 않는다.

---

## 2. 라이브러리 기준선

프로젝트의 현재 runtime dependency는 다음 범위다.

```text
discord.py >= 2.7.1, < 3
```

2026-08-24 확인 기준 PyPI의 최신 안정 버전은 `discord.py 2.7.1`이다.

Codex Dispatch는 Discord Gateway reconnect를 `discord.py`의 `Client.start(..., reconnect=True)`에 맡긴다.

---

## 3. Discord Application/Bot 생성

Discord Developer Portal에서 전용 application을 만든다.

권장 이름:

```text
Codex Dispatch
```

Application에 Bot user를 추가하고 Bot token을 발급한다.

Bot token은 Git repository, Discord 채팅, 로그에 남기지 않는다.

토큰이 노출된 경우 기존 값을 계속 사용하지 말고 Developer Portal에서 즉시 재발급한다.

---

## 4. Gateway Intent

Codex Dispatch는 일반 Discord 메시지를 Codex prompt로 사용하므로 `MESSAGE_CONTENT` privileged intent가 필요하다.

Developer Portal의 Bot 설정에서 다음을 활성화한다.

```text
Message Content Intent: ON
```

코드에서도 다음 intent만 요청한다.

```text
Guilds
Guild Messages
Message Content
```

다음 privileged intent는 사용하지 않는다.

```text
Server Members Intent: OFF
Presence Intent: OFF
```

Discord는 Message Content를 privileged intent로 취급하므로 Portal 설정과 코드 설정이 모두 필요하다. 권한이 없는 privileged intent를 요청하면 Gateway 연결이 거부될 수 있다.

---

## 5. Bot 설치 권한

전용 Discord server에 Bot을 설치한다. PHASE 7+의 `/status`, `/queue`, `/cancel`, `/mode`, `/auto` application command를 사용하려면 설치 URL에 `bot`과 `applications.commands` scope를 포함한다. Codex Dispatch는 시작 시 허용 Guild에 이 command들을 sync한다.

최소 권장 channel 권한:

```text
View Channels
Send Messages
Send Messages in Threads
Read Message History
```

향후 Codex session별 Thread 자동 생성(PHASE 4)을 위해 다음 권한도 미리 줄 수 있다.

```text
Create Public Threads
```

`Administrator` 권한은 주지 않는다.

Codex Dispatch가 사용하는 전용 channel 하나를 만드는 것을 권장한다.

예:

```text
#codex
```

---

## 6. Discord ID 확보

Discord client에서 Developer Mode를 활성화한 뒤 다음 ID를 확보한다.

```text
Guild ID
Codex channel ID
사용을 허용할 User ID
```

여러 값을 허용할 경우 comma-separated list를 사용한다.

예:

```text
DISCORD_CONTROL_CHANNEL_ID=222222222222222222
DISCORD_ALLOWED_GUILD_IDS=111111111111111111
DISCORD_ALLOWED_CHANNEL_IDS=222222222222222222
DISCORD_ALLOWED_USER_IDS=333333333333333333,444444444444444444
```

Thread ID를 allowlist에 미리 등록할 필요는 없다.

Codex Dispatch는 다음 중 하나면 허용 채널로 판정한다.

1. message channel 자체가 `DISCORD_ALLOWED_CHANNEL_IDS`에 포함됨
2. message가 Thread 안에 있고 그 parent channel이 allowlist에 포함됨

따라서 `#codex`만 allowlist에 등록하면 향후 그 아래 생성되는 Codex session Thread를 사용할 수 있다.

PHASE 4부터는 이 parent channel을 `DISCORD_CONTROL_CHANNEL_ID`에도 지정한다. 해당 값은 반드시 `DISCORD_ALLOWED_CHANNEL_IDS`에 포함되어야 한다.

---

## 7. 환경 설정

systemd 운영에서는 설정을 세 파일로 분리한다.

```text
/etc/codex-dispatch/codex-dispatch.env  # non-secret runtime config
/etc/codex-dispatch/notify.env          # non-secret notify bridge config
/etc/codex-dispatch/secret.env          # Discord token only
```

main config 예:

```text
CODEX_DISPATCH_LOG_LEVEL=INFO
CODEX_DISPATCH_DB_PATH=/var/lib/codex-dispatch/codex-dispatch.db
CODEX_ALLOWED_ROOTS=/home/your-user/project-a:/home/your-user/project-b
CODEX_DISPATCH_CODEX_BIN=/absolute/path/to/codex

DISCORD_CONTROL_CHANNEL_ID=<channel-id>
DISCORD_ALLOWED_GUILD_IDS=<guild-id>
DISCORD_ALLOWED_CHANNEL_IDS=<channel-id>
DISCORD_ALLOWED_USER_IDS=<user-id>
```

secret config:

```text
DISCORD_BOT_TOKEN=<secret>
```

systemd 운영 권장 권한:

```text
codex-dispatch.env  root:<service-group> 0640
notify.env          root:<service-group> 0640
secret.env          root:root            0600
```

Bot token은 service user가 읽을 수 있는 main/notify config나 `.env.example`에 실제 값으로 넣지 않는다. 또한 `CODEX_ALLOWED_ROOTS`에는 홈 전체가 아니라 실제 원격 제어 대상 project만 나열한다.

---

## 8. Python 설치

Codex Dispatch를 실제로 실행할 Linux 사용자로 설치한다.

예:

```bash
cd /home/your-user/codex-dispatch
python3 -m venv .venv
.venv/bin/pip install -e .
```

설치 확인:

```bash
.venv/bin/python -c 'import discord; print(discord.__version__)'
```

현재 기준 기대 범위:

```text
>= 2.7.1
< 3
```

---

## 9. PHASE 2 Host Acceptance

PHASE 11 systemd 배포에서는 Discord token이 root-only `secret.env`에 있으므로 acceptance를 root shell에서 두 파일을 함께 로드해 실행한다. 이 절차는 PHASE 2 Gateway 진단 전용이며 production service 자체는 계속 일반 service user로 실행된다.

```bash
sudo bash -c '
  set -a
  source /etc/codex-dispatch/codex-dispatch.env
  source /etc/codex-dispatch/secret.env
  set +a
  export PYTHONDONTWRITEBYTECODE=1
  cd /home/your-user/codex-dispatch
  exec .venv/bin/python scripts/discord-host-acceptance.py
'
```

정상 연결되면 다음 안내가 표시된다.

```text
Discord Gateway connected.
Send one text message from an allowed user in an allowed channel/thread.
```

허용된 Discord 사용자 계정으로 `#codex` 또는 그 하위 Thread에 임의의 텍스트를 하나 보낸다.

Bot이 같은 Channel/Thread에 다음 메시지를 보내야 한다.

```text
Codex Dispatch PHASE 2 acceptance: PASS
```

그리고 로컬 스크립트는 성공으로 종료한다.

```text
PASS: authorized inbound message received and outbound reply sent (...)
```

이 테스트는 사용자가 보낸 메시지 본문을 출력하지 않는다.

기본 전체 timeout은 120초다. 변경하려면:

```bash
.venv/bin/python scripts/discord-host-acceptance.py --timeout 300
```

---

## 10. Authorization Acceptance

정상 수신/송신 PASS 후 다음 거부 동작도 확인한다.

### 다른 사용자

`DISCORD_ALLOWED_USER_IDS`에 없는 계정의 메시지는 Codex Dispatch handler에 전달되지 않아야 한다.

### 다른 채널

allowlist에 없는 채널의 메시지는 처리되지 않아야 한다.

### DM

Bot DM은 기본 거부한다.

### Bot/Webhook

다른 Bot 또는 Webhook이 작성한 메시지는 처리되지 않아야 한다.

---

## 11. 로그

PHASE 2는 메시지 본문을 INFO 로그에 기록하지 않는다.

허용 메시지의 경우 다음 메타데이터만 기록한다.

```text
message_id
guild_id
channel_id
parent_channel_id
user_id
content_length
```

거부 메시지는 거부 이유와 message ID만 기록한다.

예:

```text
discord.message_rejected reason=user_not_allowed message_id=...
discord.message_rejected reason=channel_not_allowed message_id=...
```

Bot token은 로그에 기록하지 않는다.

---

## 12. 문제 해결

### `discord.py is required`

프로젝트 runtime dependency가 설치되지 않은 것이다.

```bash
.venv/bin/pip install -e .
```

### Gateway close code 4014 또는 privileged intent 오류

Developer Portal의 Bot 설정에서 Message Content Intent가 활성화되어 있는지 확인한다.

### 메시지는 보이지만 Bot이 처리하지 않음

다음을 확인한다.

```text
Guild ID
parent 또는 channel ID
User ID
Message Content Intent
```

특히 Discord Thread 내부 메시지라면 allowlist에는 Thread ID가 아니라 parent `#codex` channel ID를 넣어도 된다.

### Bot이 메시지를 전송하지 못함

Discord channel permission에서 다음을 확인한다.

```text
View Channels
Send Messages
Send Messages in Threads
```

---

## 13. PHASE 2 PASS 기준

다음이 모두 확인되면 Host Acceptance를 PASS로 판정한다.

- [ ] `discord.py` runtime dependency 설치
- [ ] Discord Gateway 연결 성공
- [ ] Message Content Intent 정상 동작
- [ ] 허용 Guild/Channel/User 메시지 수신
- [ ] 허용 parent 아래 Thread 메시지 수신
- [ ] 허용 Channel/Thread로 메시지 송신
- [ ] DM 거부
- [ ] 허용되지 않은 Guild/Channel/User 거부
- [ ] Bot/Webhook/system message 거부
- [ ] reconnect mode 활성화
- [ ] 메시지 본문과 Bot token이 INFO 로그에 노출되지 않음
