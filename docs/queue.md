# Agent checkpoint queue

后台脚本观察硬件/测试进度；仅在 F10 待审核、明确 Boot Failure、采集/控制异常、测试完成等检查点发送去重通知。普通 BIOS entry 在已校准 auto-apply 模式直接继续到 F10，不逐帧唤醒 Agent。

此 adapter 使用安装环境提供的命令形状：

```text
codex queue --thread EXISTING_THREAD_UUID --message CHECKPOINT_METADATA
```

该命令依赖本机 CLI 构建。启用前自行查看 `codex queue --help`，不能假定任意标准 CLI 都有这个子命令。不可用时关闭通知并人工检查状态；不开新会话、不猜测 last thread。CLI 退出零只表示 queued，不表示 Agent 已读取、审批或完成。

## 配置

在未提交的 `config/host.json` 中配置：

```json
{
  "notifications": {
    "enabled": false,
    "codex_executable": "codex",
    "retry_seconds": 30,
    "timeout_seconds": 20,
    "dry_run": false
  }
}
```

确认本机能力后再开启 enabled，并设置真实 `thread_id` UUID，或通过 `CODEX_THREAD_ID` 明确指定。缺省 disabled；PATH 不包含该 CLI 时把 `codex_executable` 改成完整本机路径。不要把开发者的用户路径/会话 ID 复制给另一台机器。

## Outbox 与证据

每条通知写到 `RUN/notifications/EVENT_HASH.json`，起始 pending；只有命令成功才改 queued。失败保留记录，worker 可 drain/retry。相同事件/证据去重，截图或状态改变才产生新 event。锁避免并发发送重复通知。

```python
from nanokvm_flow.notify import Notifier
notifier = Notifier(config)
preview = notifier.notify(run, "f10_review", "完整 F10 清单待审", dry_run=True)
pending = notifier.outbox(run)
results = notifier.drain(run)
```

只排队检查点 metadata，原图/raw artifact 保留 run。敏感字段和认证相关文本过滤；仍不得把凭据作为 summary/evidence 输入。outbox 成功不能放行 F10，只有本次请求/profile/图片绑定的独立审批才能让 `save` 消费一次确认。
