# Windows target pipeline

目标脚本在已登录 Windows 的管理员交互会话运行，负责采集与明确请求的测试。分析机通过 SSH/SCP 部署，配置与依赖见 [setup](../docs/setup.md)。导入模块不会连接硬件；真正的 capture/start-test 会读取硬件并启动程序。

## 安装与配置

部署根默认 `D:\OCBootCapture`；配置为该目录的 `config.json`，输出为 `runs/RUN_ID`。Python 3.13、PowerShell 7、HWiNFO、HWTrace、ZenTimings headless CLI、AFU、y-cruncher 使用现有目标安装，不在本包分发。

首次部署前，按实际路径编辑本地 `target/config.example.json`。host `deploy` 上传 Python/PowerShell 脚本，验证/部署独立版本目录中的 ZenTimings bundle，写目标 config、安装缺失的登录任务并 arm pending。目标已有 config 时保留路径配置，仅迁移 CLI 与固定 VT3 十次。部署不自动重启或开始测试。

手动安装 future-login 任务的等价入口（目标管理员桌面）：

```powershell
pwsh -NoProfile -File D:\OCBootCapture\Install-AtLogon.ps1 -Python C:\PATH\pythonw.exe
```

任务使用当前用户 `Interactive/Highest`，不保存密码，不配置自动登录，也不立即启动任务。已有同名任务拒绝替换。installer 的 CPU/主板检查当前固定为 9950X3D/B850M；不能通过只改 JSON 支持另一块主板。host 自动安装时 Python 默认路径为当前用户 Python313/pythonw.exe，需与实际安装匹配。

`pending.json` 为 `{"run_id":"RUN_ID"}`；无 pending 时登录任务安静退出。交互 Explorer 桌面确认后写 `desktop-ready.json`，等待 `login_delay_seconds`（当前示例 10s）。开机/桌面就绪属于不同观测；目标不负责把 SSH 可用当作 HDMI 恢复。

## 采集与收件回执

```powershell
python D:\OCBootCapture\pipeline.py capture --config D:\OCBootCapture\config.json --run-id RUN_ID
python D:\OCBootCapture\pipeline.py status --config D:\OCBootCapture\config.json --run-id RUN_ID
```

采集拒绝已有 TM5/y-cruncher/Komari 负载，检查 HWiNFO 进程与有效 SM2，共享接口可用则复用；已有进程但接口未好只等待，不重启/重复启动。仅无进程时有 Sensors-only 启动分支。ZenTimings 只执行 CLI JSON export 一次，不操作 GUI。AFU 仅 `/O` 导出唯一 32MiB dump，之后启动已有 HWTrace collector，不启动测试。

`capture.lock` 防止重复采集覆盖半成品；失败后保留现场，用新 run ID 明确重试。`state.json` 记录阶段，`failed.json` 记录失败。`manifest.json` 包括 run/boot/folder、两个文件的 name/size/sha256、collector PID/process spec；`complete.json` 最后发布并绑定 manifest hash。receiver SSH 命令不进入 manifest。

host 接收并校验所有原件、VDDP 精确请求/报告和新 receiver 样本，上传 `received.json`：

```json
{
  "run_id": "RUN_ID",
  "manifest_sha256": "EXACT_DOWNLOADED_MANIFEST_SHA256",
  "readback_verified": true,
  "telemetry_verified": {"session":"ACTUAL_SESSION","seq":2,"timestamp":"2026-11-04T04:00:00Z"},
  "files": [
    {"name":"F12d_RUN_ID.bin","size":33554432,"sha256":"ACTUAL_HASH"},
    {"name":"ZenTimings_RUN_ID.json","size":12345,"sha256":"ACTUAL_HASH"}
  ]
}
```

文件列表及顺序须与实际 manifest 相同；样例文件名不可当作约定重命名原件。PID 活着不证明接收端收到 sample。collector 若需重启，manifest 更新并返回 `awaiting_receiver`；host 要重新收取并验证新 collector 的样本、上传新回执，才能再次请求测试。

## 明确启动测试

```powershell
python D:\OCBootCapture\pipeline.py start-test --config D:\OCBootCapture\config.json --run-id RUN_ID --kind vt3
```

开始前重验回执与文件，并拒绝重复启动。测试用无触发器的一次性 Interactive/Highest 任务，避免从 SSH 启动到 Session 0。

VT3 配置：116GiB、10 次独立进程、每次最多 600s、间隔 15s。算法错误仍继续剩余启动；控制器/启动故障记录为基础设施例外，保留部分批次。每轮程序原始 log 与 console log 分开，记录 UTC/uptime/monotonic 的启动到退出时长与间隔。程序日志 Total Elapsed 不作首次错误时刻；显式错误优先于 Passed，退出码零不足以判通过，未知格式为 unknown。

`test.json` 记录启动请求，`test-result.json` 为最新结果，`test-vt3-result.json` / `test-tm5-result.json` 保留各类结果；VT3 artifact manifest 绑定十份日志与汇总。host 收齐并校验后才通知。部分批次另有完成状态，不伪称十次完成。

可选 `--kind tm5` 沿用程序已有配置；`process_started` 不证明 Absolut 已选、进入 Testing 0 或无错误，需另查实际日志与 profile。脚本不自动修改 TM5 设置。

离线验证：在仓库根运行 `python -m unittest discover -s tests -v`。这不能替代目标部署、共享内存、实际 CLI export、AFU 采集或端到端实验的验证。
