# Codex 检查点队列

[English](queue.md) | 简体中文

后台 worker 等待硬件/测试，只在 F10 审核、明确 Boot Failure、采集/控制异常或测试结束时，把简短检查点排入已有 Codex chat。普通校准根页 auto-apply 直接到 F10，不单独发入口通知。Agent 可以结束当前回复，不必一直轮询。

[官方 CLI 参考](https://learn.chatgpt.com/docs/cli/reference)说明了向既有会话入队的命令。本项目调用：

```text
codex queue --thread SESSION_UUID --message CHECKPOINT_METADATA
```

官方 CLI 支持 UUID 或精确名称，但本项目仅接受明确 UUID。不使用 `--resume`、`--last`，不新建或猜测 chat。保持负责审核的 chat 可用；退出零仅代表接受入队，不等于 Agent 已读取/处理，也不是 F10 批准。

## 自己填写 session ID

复制 `config/host.example.json` 后，在**本地** `config/host.json` 编辑：

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

1. 选择负责这轮图像/结果审核的 Codex chat。
2. 从客户端取得真实 UUID，或使用它启动的 shell 中提供的 `CODEX_THREAD_ID`。不要取不相关终端的值，也不按标题猜测。
3. **把 `thread_id` 的空字符串替换成真实 UUID**。不要填 PID、NanoKVM ID、API Key、标题或字面占位符 `SESSION_UUID`。
4. 检查 `codex queue --help` 及 CLI 登录环境；PATH 不含程序时填写完整 `codex_executable` 路径。
5. 配置后再设 `enabled=true`。人工查看时可保持 false，本地配置不提交 Git。

也可保留空 `thread_id`，在启动 runner 的进程环境明确设置 `CODEX_THREAD_ID`；非空配置值优先。脚本校验 UUID 格式，但无法证明填的是正确 chat。示例没有别人的会话 ID。

`retry_seconds` 为重试间隔，`timeout_seconds` 为调用超时，`dry_run=true` 仅预览。adapter 复用本机 CLI 登录，不新增 OpenAI API Key/API client。文档能力不代表任意旧版 CLI 已支持相同选项。

## Outbox 与验证边界

通知写到 `RUN/notifications/EVENT_HASH.json`，pending 起始，CLI 成功才为 queued。失败保留供 worker drain/retry；相同事件/证据去重，新证据才生成新 event，文件锁避免并发重复。

```python
from nanokvm_flow.notify import Notifier
notifier = Notifier(config)
preview = notifier.notify(run, "f10_review", "完整 F10 清单待审", dry_run=True)
pending = notifier.outbox(run)
results = notifier.drain(run)
```

以上使用已有本地 run/config。要获得 dry-run payload，enabled 需为 true，否则返回 disabled。dry-run 不验证目的 chat 是否存在、不发送、不标为 queued；`drain` 在 enabled 时会真实发送待处理通知。

只传检查点 metadata，原始截图与文件留在 run。不把凭据放进 summary/evidence。queue 成功不能放行 F10，仍需 Agent 审完所有页面并写本次 request/profile/截图 hash 绑定的审批，`save` 才消费一次确认。
