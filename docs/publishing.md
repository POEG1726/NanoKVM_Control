# GitHub 发布准备

开发仓库含旧实现/研究导出历史，本地 `AGENTS.md`、`handoff.md`、`config/scans/` 记录机器与实验上下文。不能把 ignore 当作历史清除，也不直接推当前开发分支或 `--all/--tags`。

用白名单导出全新源码快照，保留原工作树、运行产物、授权和归档：

```powershell
python tools/prepare_github.py --output .local/github/NanoKVM_Control
```

输出必须不存在，工具不覆盖已有目录，不读取密钥目录，不连接目标。只包含主 README、MIT LICENSE、公共文档、程序/测试、CI、配置示例和两个校准 profile。原始机器配置、扫描队列、历史 handoff/归档、截图、dump、波形、数据库、第三方二进制和 Git 历史都排除。profile 的外置路径仅在副本中替换；开发原件/hash 不变。

`publication-manifest.json` 记录每个发布文件的字节数和 SHA256（不包含清单自身与 `.git`）。这是可审计白名单，不是对任意未来新源码的保密保证；导出前仍要审查新增文件。不要以 `git add -A` 代替该范围定义。

在输出目录进行离线验证并建立新的根提交：

```powershell
$publishRoot = '.local/github/NanoKVM_Control'
Push-Location $publishRoot
try {
    python -m unittest discover -s tests -v
    python -m compileall -q nanokvm_flow target tools
    git init -b main
    git add .
    git diff --cached --check
    git commit -m 'Initial direct NanoKVM workflow'
} finally { Pop-Location }
```

建议目标：`POEG1726/NanoKVM_Control`，MIT（用户已确认）。当前整理阶段只建立本地待发布版本，不创建远端、不推送。确认仓库可见性与精确发布提交后，在这个输出仓库操作：

```powershell
# PUBLIC 或 PRIVATE 按发布决定选择，以下示例是 public
gh repo create POEG1726/NanoKVM_Control --public --source .local/github/NanoKVM_Control --remote origin
git -C .local/github/NanoKVM_Control push -u origin main
```

以后源码变化重新导出到新目录并审查差异，再更新独立发布 checkout。不要把开发仓库含旧历史的 branch 合入发布仓库。CI 只运行离线测试；GitHub 托管检查与实机验证要另记实际结果。
