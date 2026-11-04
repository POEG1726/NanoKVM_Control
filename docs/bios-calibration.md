# B850M F12d 校准参考

历史设备：9950X3D / B850M AORUS ELITE WIFI6E ICE / F12d / 4×32GiB，1920×1080 BIOS。这里只记录已采样路径；不证明新设备当前设置，不提供自动执行许可。原始图片和 hash 在操作者的本地 run，公开包不附带。

|字段 / 动作|从已确认 Advanced / Tweaker 根页开始的键序|证据边界|
|---|---|---|
|重新锚定末行|End → CPU/VRM Settings|不依赖上次选中行|
|Advanced Voltage Settings|End → Up1 → Enter|子菜单首项 VDDP Voltage|
|VDDP|进入子菜单 → Home → 直接输入 `0.900` → Enter|单位 V，不是 900mV 字符串；不先打开下拉|
|VSOC|End → Up6 → Enter|只查看，不是当前宏可执行的电压扫描|
|DDR VDD|End → Up4 → Enter|只查看|
|DDR VDDQ|End → Up3 → Enter|只查看|
|保存 Profile8|根页 Right4 → End → Up1 → Enter；列表 End → Up2 → Enter|核对 Save Profiles 与实际槽名，普通覆盖保留 prefilled 名称|
|F10|固定宏完成后打开|停在确认前，完整清单与 Profile Saved/槽名一起人工审核|

候选 slot 是 `voltage_slots_mv` 数组下标，不是 BIOS 存档槽或菜单下拉位置。5mV profile 的 slot20=1000mV，slot25=1025mV；粗扫 profile 的 slot2=1000mV。粗扫保留 1100mV 作为历史候选，不因此扩大当前运行授权。

Profile8 既有名称为 `agent-tune` 时，普通覆盖只确认原名，不清空/重输。`profile_storage.initialize_name=true` 专用于明确首次初始化或名称修复。必须同时留 Profile Saved 与重新打开列表的槽名证据。

NanoKVM 图像可能落后一帧：client 丢弃一次原生截图，间隔 150ms 再保留第二次。仍需检查实际图像，不能凭文件标签、OCR 字段或静态菜单图当作完整 F10 清单。

只有已校准根页严格匹配时可 auto-apply；子菜单、未知图、missed entry、Boot Failure 交给 Agent。明确 Boot Failure 的 Load Profile8 确认路径需现场核对；历史 Save 路径不能证明恢复流程已验证。黑屏不自动恢复/重启。

BIOS 画面证明待保存请求和导航，后续仍需 dump/ZenTimings/遥测。5mV profile 必须有精确 saved-request reader；SMU 读数是独立 sanity check，不是相邻候选身份或物理电压测量。
