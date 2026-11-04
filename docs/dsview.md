# DSView native capture control

使用[我的 DSView-Mod 项目](https://github.com/POEG1726/DSView)中实现 capture-control 的构建，匹配其 runtime；旧版/任意 release 未必有该接口。此仓库只有 CLI adapter，不含 DSView/decoder 源码或二进制。设备、频道与 Stream 模式需事先按真实接线配置。

## CLI / IPC

```powershell
& 'D:\Tools\DSView-Mod\DSView-core.exe' --capture-status
& 'D:\Tools\DSView-Mod\DSView-core.exe' --capture-start 'D:\Captures\unique-run-id' --capture-format espi
& 'D:\Tools\DSView-Mod\DSView-core.exe' --capture-stop
```

GUI 主实例事先运行并拥有设备，CLI 使用同用户 QLocalServer/QLocalSocket newline JSON（schema1），不创建第二个硬件 session，也不加载/保存用户 profile。`--capture-control-name NAME` 可选择隔离 endpoint；主实例和 CLI 必须匹配。adapter 直接调用 core 并只给子进程 PATH 添加匹配的 `runtime/bin`；手动调用也要准备对应 DLL 搜索路径，普通 GUI launcher 不保证 stdout 可读取。

配置来自 `config/dsview.json`；模板见 `config/dsview.example.json`。start 明确请求开始，不是 toggle；要求已配置 Stream 模式，设置 50MHz/Loop 和新的 output root。兼容的既有 capture 可复用，不兼容/不完整的 recorder 保留原样并报错。

默认 `espi` 使用 DSView 既有 eSPI recorder/decode/save 实现和频道名，初始 Quad width4；可选 `csv` 是既有压缩原始波形。adapter 不猜测接线，不替换 decoder。确认记录格式与物理总线/decoder 设置相符。

start 被接受就返回 `start_requested=true` 与当前状态，不等待样本、header、文件增长或可解码事务。stop 要等 device/session/recorder 停止、queue 排空，返回 `flushed=true`，不能留下 `.part`。命令超时保留现场，不自动重启或强杀 recorder。

## 顺序与回执

外部脚本在 reboot/F10 前发 start，在**本 run 的 Windows 交互桌面确认**后发 stop。DSView 只负责采样、原有解码和存盘，不知道 BIOS/POST/电压/测试状态。采样错误独立记录/通知，不阻断机器控制，也不改变测试结论。

`nanokvm_flow.dsview.ensure_capture_started` 返回 `capture_start_requested` 或 `capture_reused`；名字兼容旧调用方，不代表已经收到有效数据。`stop_capture` 验证完整落盘与 queue drained。run 目录保留 `dsview-start.json` / `dsview-stop.json`，重试保留旧回执；输出根必须唯一，不覆盖原始文件。

状态可包括 `working/device_collecting/device_ready/stream_mode/loop_mode/sample_rate_hz`、`recorder_running/ready/failed`、`header_ready/samples_written/bytes_written/queued_bytes/segments/format/output_directory`。它们是采样观测，不是 POST 成功或真实训练结果。空闲 eSPI 总线可无 semantic rows，不能据此决定是否重启。

## 升级和验证边界

旧运行实例不会因换路径获得 IPC 功能。先确认旧 recorder 已停止、文件完整落盘且 queue drained，再正常退出、更换构建并启动；不要强杀活动/不完整 recorder。完全落盘后的重复内存波形可以正常 discard，无需额外再存一次。

离线 adapter tests 使用 fake，不启停真实采样。编译成功、隔离 IPC 测试、只读 status、物理 start/stop 与端到端实验分别记录；本次文档整理不增加实机验证结论。
