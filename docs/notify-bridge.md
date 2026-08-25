# Codex Notify Bridge

PHASE 3 introduces a local Unix Domain Socket bridge between Codex CLI's external `notify` callback and the long-running Codex Dispatch orchestrator.

## Flow

```text
Codex CLI
  |
  | external notify JSON
  v
bin/codex-notify.py
  |
  | length-prefixed JSON over Unix Domain Socket
  v
NotifyServer
  |
  | validated NotifyEvent
  v
Codex Dispatch orchestrator
```

The bridge is intentionally local-only. It opens no TCP port.

## Environment

The orchestrator and the Codex notify client must resolve the same socket path.

```text
CODEX_ALLOWED_ROOTS=/home/your-user/project-a:/home/your-user/project-b
CODEX_DISPATCH_NOTIFY_SOCKET=
CODEX_DISPATCH_NOTIFY_MAX_BYTES=262144
CODEX_DISPATCH_NOTIFY_TIMEOUT_SECONDS=0.75
```

If `CODEX_DISPATCH_NOTIFY_SOCKET` is empty, the default is:

```text
$XDG_RUNTIME_DIR/codex-dispatch/notify.sock
```

or, when `XDG_RUNTIME_DIR` is unavailable:

```text
/run/user/<uid>/codex-dispatch/notify.sock
```

The socket parent directory must be owned by the Codex Dispatch service user and have mode `0700`. An existing shared directory such as `/tmp` is rejected. The socket itself is created with mode `0600`.

`CODEX_ALLOWED_ROOTS` is mandatory from PHASE 3 onward. Every incoming `cwd` is resolved with the filesystem and must remain inside one of the allowed roots after symlink resolution.

## Codex configuration

For a source checkout, configure Codex external notify to invoke:

```toml
notify = [
  "python3",
  "/home/your-user/codex-dispatch/bin/codex-notify.py"
]
```

When the project is installed into a virtual environment, the packaged command may be used instead:

```toml
notify = [
  "/home/your-user/codex-dispatch/.venv/bin/codex-dispatch-notify"
]
```

The notify client is deliberately short-lived. It validates that the argument is a JSON object, enforces the payload size limit, connects to the local Unix socket with a short timeout, writes one framed payload, reads one status response, and exits.

Successful server responses are:

```text
ACK
DUPLICATE
```

Both produce exit code `0` from the notify client. Local socket failures use exit code `69`; invalid input/configuration uses `65`; protocol/server rejection uses `70`.

## Accepted event schema

PHASE 3 accepts only:

```text
type = agent-turn-complete
```

Required fields:

```text
type
thread-id
turn-id
cwd
```

Optional/validated fields currently carried forward:

```text
client
input-messages
last-assistant-message
```

`thread-id` and `turn-id` must be non-empty bounded strings without control characters. `cwd` must be absolute, must exist, must resolve to a directory, and must be inside `CODEX_ALLOWED_ROOTS` after symlink resolution.

Prompt and assistant-message bodies are kept only in the in-memory `NotifyEvent` for later phases. They are not written to normal INFO logs.

## Framing

The client sends exactly one event per Unix socket connection:

```text
4-byte unsigned big-endian payload length
JSON UTF-8 bytes
```

The server rejects zero-length, oversized, incomplete, malformed UTF-8, malformed JSON, and non-object payloads before calling the orchestrator handler.

Default payload maximum:

```text
262144 bytes
```

## Duplicate handling

PHASE 3 performs bounded in-memory duplicate suppression using:

```text
(thread-id, turn-id, type)
```

A successfully handled event is remembered. A repeated event returns `DUPLICATE` and is not delivered to the handler again.

Handler failures are not marked as duplicates, so a later retry can still be processed.

Persistent idempotency belongs to PHASE 4 and will replace this process-local cache with the SQLite `notify_events` table.

## Single-instance protection

At startup, Codex Dispatch handles socket paths as follows:

- no socket: bind normally;
- stale Unix socket with no listener: remove it and bind;
- active Unix socket: refuse startup;
- regular file/symlink target that is not a socket: refuse startup;
- insecure or foreign-owned parent directory: refuse startup.

This prevents a second Codex Dispatch process from silently replacing an active instance's socket.

## Host acceptance

The DevSpace MCP runtime cannot access the real Codex user's home/session environment, so one real host acceptance run is required on the VPS under the same Linux user that runs Codex.

Prepare the non-secret runtime and notify environment:

```bash
cd /home/your-user/codex-dispatch
set -a
source /etc/codex-dispatch/codex-dispatch.env
source /etc/codex-dispatch/notify.env
set +a
```

`secret.env` is not required for the local notify acceptance and should not be loaded.

Start the one-shot acceptance receiver:

```bash
PYTHONPATH=src python3 scripts/notify-host-acceptance.py
```

In another terminal, run Codex in a workspace covered by `CODEX_ALLOWED_ROOTS` and complete one turn. Codex must be configured to invoke `bin/codex-notify.py` or the installed `codex-dispatch-notify` entrypoint.

PASS output includes:

```text
Codex Dispatch PHASE 3 acceptance: PASS
type=agent-turn-complete
thread-id=...
turn-id=...
cwd=...
client=...
```

The host acceptance is complete only when the actual installed Codex CLI produces that event; a synthetic fixture is not sufficient for the final PHASE 3 host verification.

## Automated verification

Repository tests cover:

- client/server round trip;
- payload framing;
- malformed JSON;
- oversized frames;
- required event fields;
- unsupported event types;
- workspace allowlist;
- symlink escape rejection;
- duplicate suppression;
- handler retry behavior;
- private socket permissions;
- active second-instance rejection;
- non-socket path protection;
- client-side input and size validation.

Run:

```bash
bash scripts/test.sh
```
