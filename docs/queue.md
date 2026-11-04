# Codex checkpoint queue

[English](queue.md) | [简体中文](queue.zh-CN.md)

The worker waits for boot/capture/test progress in the background. At meaningful
checkpoints it queues a short message to the existing reviewing Codex chat:
F10 review, explicit Boot Failure, capture/control exceptions or test completion.
Ordinary calibrated auto-apply entry continues to F10 without a separate entry
notification. This avoids keeping an agent turn open for hardware polling.

The [official Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference)
documents `codex queue` for an existing session. This repository uses:

```text
codex queue --thread SESSION_UUID --message CHECKPOINT_METADATA
```

The official CLI can accept an exact session name; this adapter intentionally
requires an explicit UUID. It does not pass `--resume`, select `--last`, create a
chat or guess a destination. Queue acceptance is not proof of delivery/handling
and never approves a BIOS change. Keep the intended reviewing chat available.

## Fill in your own session ID

Copy `config/host.example.json` to `config/host.json`. Edit the **local** file:

```json
{
  "notifications": {
    "enabled": false,
    "thread_id": "",
    "codex_executable": "codex",
    "retry_seconds": 30,
    "timeout_seconds": 20,
    "dry_run": false
  }
}
```

1. Choose the Codex chat that will inspect this experiment's screenshots/results.
2. Obtain that chat's actual UUID from the client, or from `CODEX_THREAD_ID` in a
   shell launched by that chat when the environment variable is provided. Do not
   use an unrelated terminal's value or infer it from the title.
3. Paste the UUID into the empty `notifications.thread_id` string. Use a chat UUID,
   not a PID, NanoKVM ID, API key, title or the literal `SESSION_UUID` placeholder.
4. Run `codex queue --help` and confirm the executable/login environment is the
   intended local installation. Use an absolute `codex_executable` path if needed.
5. Set `enabled` to `true` after configuration. Leave it false for manual review
   without notification delivery. Do not commit your local configuration.

Alternatively, leave `thread_id` empty and explicitly set the runner process's
`CODEX_THREAD_ID`. A nonempty config value takes precedence. The runner validates
UUID syntax but cannot prove that a UUID is the right reviewing chat.

|Setting|Meaning|
|---|---|
|`enabled`|Delivery opt-in; false by default|
|`thread_id`|Your reviewing chat UUID; blank template, with environment fallback|
|`codex_executable`|Local CLI command or absolute executable path|
|`retry_seconds`|Minimum delay before retrying a pending notification|
|`timeout_seconds`|Timeout for each CLI invocation|
|`dry_run`|Build a preview without invoking the CLI; not a delivery test|

No new OpenAI API key or API client is used by this adapter. It relies on the
CLI's existing login and local session availability. A newer reference page is
not a guarantee that an older installed binary has the same command/options.

## Outbox, retries and dry-run

Records live in `RUN/notifications/EVENT_HASH.json`. They start pending; CLI
success changes them to queued. Failures stay pending for worker drain/retry.
Matching event/evidence is deduplicated; a meaningful evidence change creates a
new event. File locks prevent simultaneous duplicate sends.

For an existing local `run` and configured `config`, the Python API supports:

```python
from nanokvm_flow.notify import Notifier
notifier = Notifier(config)
preview = notifier.notify(run, "f10_review", "Review the complete F10 list", dry_run=True)
pending = notifier.outbox(run)
results = notifier.drain(run)
```

Delivery must be enabled to obtain the dry-run payload; otherwise the result is
disabled. Dry-run does not send, validate the destination's existence or mark a
record queued. `drain` performs real delivery for enabled pending notifications.

Only checkpoint metadata is sent; original screenshots and raw files remain in
the run folder. Sensitive-field filtering is a last defense, not permission to
supply credentials in summaries/evidence. Successful queueing does not release
F10. An agent must inspect all pages and produce the request/profile/image-hash
bound decision before `save` can consume confirmation once.
