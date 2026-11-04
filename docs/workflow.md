# 命令、审核与运行证据

常规入口：`./run.ps1 [--config config/host.json] COMMAND --run RUN`。全局 `--config` 放在子命令之前。run ID 使用字母/数字/下划线/短横线，重复 ID 不覆盖。

## 命令分组

|命令|行为|
|---|---|
|`init --profile FILE --slot N`|复制 profile 快照并建立 run；纯本地，不连接硬件|
|`status`|读本地状态|
|`deploy` / `arm`|SSH 上传脚本/CLI/配置/任务并写 pending，或只写下次登录 run；会改变目标文件/任务|
|`enter` / `wait-video`|原生截图观察；`enter` 同时重复 Delete，本身不重启|
|`shot --label NAME`|原生 screenshot；F10 标签可能刷新审核请求，旧审批须重做|
|`keys` / `type-value` / `profile-name`|受状态限制的校准命令，不是通用自动调参入口|
|`anchor --image-sha256 SHA`|Agent 确认最新截图锚点，绑定实际图像|
|`calibrate --profile FILE`|在允许状态换成已校准 profile，不猜测导航偏移|
|`apply`|固定键序编辑候选，保存 Profile8，打开 F10 暂停|
|`review-page`|滚动/截图 F10 页面，更新审核请求|
|`review --decision FILE`|检查 Agent 决策与当前请求和证据完全匹配|
|`save`|再次验证并消费一次审批，开始采样请求后只发送一次 Enter；若发送不确定，不重放|
|`monitor --kind vt3 --background`|后台观察桌面/证据/遥测/测试/回收及 outbox|
|`monitor --bootstrap`|首次部署例外路径，SSH 恢复后部署并显式启动一次采集，不是普通入口要求|
|`postboot --kind vt3`|前台完成 boot 后证据/读回/回执/启动；不替代完整后台测试完成监视|
|`cancel-review`|撤回 F10 审批并退出对话框；之后重新锚定/采图/审核|

首次从 Windows 进入固件可用：

```powershell
python -m nanokvm_flow.boot_entry --config config/host.json --run RUN --firmware-ui
```

此命令请求一次 Windows `/r /fw`；不发 Delete，不自动重试。省略 `--firmware-ui` 的入口会重启后以目标 250ms 间隔发送 Delete。两种入口都保留 STOP、实际图像/OCR 检查和 Windows missed-entry 检测。`bios_entry_auto_apply=true` 或 `--auto-apply` 只在严格匹配已校准 Tweaker 根页时继续固定宏，普通成功入口不排队通知，仍停 F10。宏不校验所有既有 BIOS 参数，固定项还需 dump/JSON 的后续核对。

## F10 审核

`runs/RUN/review_request.json` 给出 `request_sha256`、`profile_sha256`、候选 slot、`expected_changes` 和证据。Agent 亲自检查完整变更清单、滚动页、Profile Saved 与 Profile8 名称证据。额外变更或缺页就是拒绝。

决策文件格式如下；占位值必须按本轮实际请求与检查结果填写，不能由脚本照抄请求自批：

```json
{
  "request_sha256": "ACTUAL_REQUEST_SHA256",
  "decision": "approve",
  "observed_changes": [],
  "all_pages_reviewed": true,
  "profile_storage_verified": true,
  "reviewer": "ACTUAL_REVIEWER",
  "reason": "实际读图发现的项目、起止值与页数，以及 Profile8 保存证据"
}
```

`observed_changes` 必须填入实际观察到并与 `expected_changes` 精确相等的对象数组。只执行 `review` 不发送 Enter；`save` 在最后时刻复核 hash，先标记审批已消费，再发确认。任何新图/新请求/计划变化需重新审核。queue 成功不是审批。

## 证据与顺序

1. 本地 `state.json` 固定 profile/slot；`events.jsonl` 记录键序与时刻；`NNN_LABEL.jpg` 和 SHA 记录实际画面。
2. DSView 开始请求在重启/F10 前，不等待总线数据增长。桌面与本次 boot 关联后停止/排空，`dsview-start/stop.json` 记录回执；错误单列。
3. `boot-timing.json` 与 HDMI watcher 保留有效图像恢复的观测边界；`desktop-ready.json` 记录 Windows 交互会话，不能用 SSH 首次连通代替 HDMI 恢复。
4. 目标先检查桌面、等待配置延迟（当前 example 10s），复用 HWiNFO、单次 ZenTimings CLI、唯一 AFU dump、HWTrace collector。
5. host 下载 manifest 和两个原始文件，逐一校验 hash/size、VDDP 请求/报告及新 receiver sample；manifest 绑定的 `received.json` 放行测试。
6. VT3 十次独立启动，每次单独程序日志、控制台日志与时钟字段。算法错误不结束余下启动，控制器/基础设施错误另报。收齐十份日志及 summaries/manifest、校验完整性后才完成通知。

详细目标文件协议见 [target README](../target/README.md)。原始日志、BIOS dump、SQLite、波形和审批都留本地，不提交 GitHub。

## 停止与恢复

向 `runs/RUN/STOP` 写文件停止本地 entry/worker/watch 等支持 STOP 的循环；不停止目标测试，不重启，不撤销已发送按键。不是所有前台单步操作都有 STOP 检查。不要删除 STOP 自动重放一次不确定的保存/重启动作。

黑屏或 SSH 不通只是等待；无总 POST 超时，无自动 reset/reboot retry。明确 Boot Failure 时暂停给 Agent 检查，Profile8 加载确认必须现场核对。恢复不是自动把下一候选发出去。测试完成也不会自行推进电压或把短测通过判为稳定。

候选 profile 是参数与校准记录，`config/scans/` 是本地实验计划。当前 10mV-first/5mV midpoint 队列和运行历史不分发为别人的执行授权，低于 900mV 的扩展仍不在当前执行范围。
