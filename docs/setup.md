# Prerequisites 与双机部署

## 分工

分析机运行本仓库、NanoKVM MCP client、DSView-Mod、逻辑分析仪和本地 OCR。目标 Windows 运行 HWiNFO、HWTrace collector、ZenTimings CLI、AFU、压力测试与 `target/` 登录任务。HWTrace 接收端可放在分析机或单独机器；目前 runner 要能通过本地路径只读其 SQLite。

不要把分析机当作采集寄存器的目标。安装/配置与真实运行是不同步骤；完成本说明不代表硬件流程已验证。

## 分析机

安装 PowerShell 7、Python 3.12+（示例统一使用 3.13）、Git、Windows OpenSSH Client。创建 `.venv` 并安装根目录 `requirements.txt`，不需要 Python NanoKVM wrapper 或旧 gateway。OCR 使用系统 WinRT，需可用的英文 OCR 语言组件；脚本用 Windows PowerShell 的 WinRT bridge，runner 用 `pwsh` 调 SSH 命令。

将 `config/host.example.json`、`config/dsview.example.json` 复制为本地配置。相对路径以项目根目录为基准；`run.ps1` 自动进入根目录，直接运行 Python 模块时自己先切换工作目录。配置中不做通用环境变量插值，填写真实路径。

## SSH key 登录

目标安装并启用 Windows OpenSSH Server，允许 SSH/SCP 和远端 `pwsh`。本项目不自动安装服务或修改防火墙。复用已有 key 与认证 provider，不把私钥复制到项目。首次部署可按 [Microsoft OpenSSH key authentication](https://learn.microsoft.com/windows-server/administration/openssh/openssh_keymanagement) 安装**公钥**；管理员账户注意文档中的 administrators_authorized_keys 路径及 ACL。身份由用户/管理员配置，脚本不读取凭据目录。

可在自己的 OpenSSH config 中维护别名，例如：

```sshconfig
Host oc-target
    HostName TARGET_ADDRESS
    User TARGET_USER
    IdentitiesOnly yes
```

实际 IdentityFile/agent 在已有认证配置中管理。主机指纹须经核对后加入 known_hosts，不禁用 host-key 检查。`host.json` 的 `ssh_target` 使用 `oc-target`，不要写 shell 命令或内嵌连接参数。代码以 `BatchMode=yes` 执行，不会弹密码提示。

部署前可在已授权的目标上自行检查：

```powershell
ssh -o BatchMode=yes oc-target 'pwsh -NoProfile -Command "$PSVersionTable.PSVersion.ToString()"'
```

还要确认 SCP 双向传输可用、目标 Python 路径正确，SSH 登录身份能管理当前用户的 Interactive/Highest 计划任务。SSH 可用不等于 Windows 交互桌面已就绪。

## NanoKVM

接好目标 HDMI 和 USB HID，启用 [NanoKVM 原生 MCP](https://wiki.sipeed.com/hardware/en/kvm/NanoKVM_PCIe/mcp.html)，将页面上的 Endpoint 填入 `mcp_url`。client 要求 Streamable HTTP 与三个工具：`cube_screenshot`、`cube_press_keys`、`cube_type_text`。不同设备/固件需核对能力，不能只凭设备名称假定兼容。

通过安全的既有环境注入 `NANOKVM_API_KEY`，或在**未提交**的 host 配置中设置 `connection_provider` 为 `本地脚本.py:函数名`，函数返回 `(url, key)`。不把 key 放进 JSON、命令示例、截图说明或提交。

仅取图用 native `cube_screenshot`。POST 阶段 Delete 目标间隔 250ms；暂态 HID 错误不意味着失败。MCP busy/手动控制冲突应作为例外检查，不自动抢占、复位或重启设备。

## 逻辑分析仪与 DSView-Mod

使用[我的 DSView 项目](https://github.com/POEG1726/DSView)，准备带 capture-control CLI/IPC 的构建与匹配 runtime。不能用旧 GUI 版假定这些开关存在。分析仪需支持 Stream/Loop 和当前 50MHz 配置，事先核对主板 eSPI 测点、电压、地线、CS/CLK/IO 频道映射；仓库不会猜测接线。

事先在 DSView 中配置设备与频道，保持同一 Windows 用户下的 DSView 实例运行；外部脚本调用 `DSView-core.exe`，不以 GUI 点击切换采样。设置 `config/dsview.json` 的 core 路径与独立输出根。详见 [dsview.md](dsview.md)。

重启/F10 前发 start 请求，桌面确认后发 stop，stop 必须确认 recorder 已停止、queue 已排空并全部落盘。不等待采样数/header/output 增长才准开机；空闲总线无解码行是允许的。capture 错误不改变 BIOS/测试结果。活动 recorder 不强杀，完整落盘后可正常退出并丢弃重复的内存波形。

## HWiNFO 与 HWTrace

目标安装 [HWiNFO64](https://www.hwinfo.com/download/)，配置 Sensors 模式、Shared Memory Support 与 Windows 登录自启，并确保所用版本/许可允许所需的共享内存持续时间。pipeline 检查 `Global\HWiNFO_SENS_SM2` 的结构；已有进程时只等接口，不点击/重启或启动第二实例。缺进程时有 Sensors-only 启动分支；正常安装应使用现有自启实例。

HWTrace 安装、collector/filter 与接收端配置直接参见[我的 HWTrace 项目](https://github.com/POEG1726/HWTrace)。目标 `hwtrace_root/collector.json` 至少要有 `filter`、`ssh_command`，可指定 `executable`、`process`、`grace_seconds`；接收命令只保存在既有目标配置，不复制到 run manifest 或本仓库。`process` 覆盖 y-cruncher 和实际子进程（当前为 `24-ZN5 ~ Komari.exe`），可选 TM5 同理。

host 的 `telemetry_db` 指向该接收端 SQLite 文件。runner 查询新 collector PID 对应 session 的新 sample；仅进程存活不能授权开测。collector 超时/退出后如重启，manifest 会更新，须重新收件并核对新 receiver sample 后回执。

## ZenTimings headless CLI 与精确请求解析器

这两项是当前实验的**外置自建组件**，公开包不内置，不能仅 `pip install` 获得：

1. ZenTimings bundle：使用[我们的 ZenTimings-Mod fork](https://github.com/POEG1726/ZenTimings-Mod)，下载 [headless release](https://github.com/POEG1726/ZenTimings-Mod/releases/latest) 并验证校验和。host 的 `zentimings_cli_bundle` 目录包含 `build-manifest.json` 和恰好四个文件：`ZenTimings.Export.exe`、`ZenStates-Core.dll`、`inpoutx64.dll`、`ZenTimings.Export.exe.config`。manifest 的 `files` 为 `{name,size,sha256}` 数组，记录这四个文件。`deploy` 先校验，再复制到目标以 manifest hash 命名的版本目录并核对传输 hash。普通 upstream GUI 不是这个 CLI 的替代；详见 [integration](zentimings-mod.md)。
2. 精确 saved-request reader：5mV profile 的 `readback_verification.saved_request_reader` 指向平台专用 `verify_vddp_request.py`，连同它的 NVAR/FV 解析依赖。接口为 `python READER --dump FILE --expected-mv INTEGER`，stdout 为 JSON，必须返回 `ok/exact_match/manual_match=true`、`expected_mv`，以及 `dump.sha256/size/unchanged_after_verification=true`。当前 F12d 检查 Tweaker/AOD 请求与 Manual selector；活跃 bank 选择仍是离线推断，不是物理电压测量。

开发工作树保留原路径；发布导出将 reader 路径替换为 `external/bios-readback/verify_vddp_request.py`。准备对应工具后才运行精扫。若没有，保持 readback gate 失败并停止，不把 `require_exact_saved_requests` 改为 false 来绕过。目标 JSON 必须包含 `static.system/memory/spd`、`config.timings/aod/apob`、`readings.power_table/sensors/dimm`，用于完整证据与 SMU 读数交叉检查。ZenTimings 不设 GUI/logon autostart，只按 run 执行一次 CLI。

## AFU、压力测试与目标配置

复用目标上已存在且匹配平台的 AFU，仅使用 `/O` 输出唯一 32MiB dump。不下载替换驱动，不刷 BIOS，不恢复 dump，不发寄存器写入。

从 [y-cruncher 官网](https://www.numberworld.org/y-cruncher/) 单独取得工具。当前配置使用 0.8.7：116GiB 是这套 128GiB 机器的实验配置；更换容量、版本或子进程名必须重新审核。VT3 固定十次独立启动，每次最长 600s，间隔 15s；记录 UTC/uptime/monotonic 的启动至退出时长，不把日志 Total Elapsed 当作首次出错时间。TM5 可选，runner 不改它的 saved profile。

部署前在本地编辑 `target/config.example.json` 的 AFU、HWiNFO、HWTrace、测试工具路径与目标身份。首次 `deploy` 使用此文件生成目标 `config.json`；已有 `config.json` 时保留既有路径，只迁移 ZenTimings CLI 配置并固定 VT3 十次。修改本地 example 不会覆盖目标已有配置。登录任务当前还硬编码这块 B850M/9950X3D 检查；改主板不能只改 JSON。

## 部署检查表

- [ ] 本地 venv、CLI help 和离线测试通过；本地配置不在待提交清单。
- [ ] 经授权确认 NanoKVM MCP 与 SSH/SCP；正确目标身份、交互桌面、管理员计划任务权限。
- [ ] 校准 BIOS 根页、字段输入单位、Profile8 与完整 F10 审核；不复用旧光标。
- [ ] HWiNFO 已自启且 SM2 有效，HWTrace 接收端真实收样，测试进程列表匹配。
- [ ] ZenTimings CLI bundle 和 exact reader 准备完毕；AFU/test 路径准确。
- [ ] 分析仪接线与 DSView CLI/IPC 已验证，输出目录唯一，原始数据不会覆盖。
- [ ] 明确本轮候选和固定项；历史 profile、授权与扫描队列分开管理。

离线测试、只读连通性、菜单校准和一次完整实机实验分别记录，不能彼此替代。
