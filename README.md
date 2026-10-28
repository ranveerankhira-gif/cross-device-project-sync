# 跨设备项目同步 Skill

**换电脑时，把项目文件、允许迁移的 Codex 对话和 Git 开发状态分别搬对，而不是把整台机器稀里糊涂复制过去。**

`cross-device-project-sync` 是一个面向 Windows 与 Codex 的显式调用 Skill。它区分完整项目备份、对话接力和 Git 开发同步，并用清单、SHA-256、不可变 Release 与事务式恢复验证迁移结果。

> Verifiable Windows project backup, Codex conversation migration, transactional restore, and Git synchronization across devices.

[![Skill](https://img.shields.io/badge/skill-1-2563eb)](#核心能力)
[![Latest release](https://img.shields.io/github/v/release/ranveerankhira-gif/cross-device-project-sync?label=release)](https://github.com/ranveerankhira-gif/cross-device-project-sync/releases/latest)
[![Validate](https://github.com/ranveerankhira-gif/cross-device-project-sync/actions/workflows/validate.yml/badge.svg)](https://github.com/ranveerankhira-gif/cross-device-project-sync/actions/workflows/validate.yml)
[![Platform](https://img.shields.io/badge/platform-Windows-0078d4)](#运行要求)
[![Explicit only](https://img.shields.io/badge/invocation-explicit--only-d97706)](#安全边界)
[![License: MIT](https://img.shields.io/badge/license-MIT-16a34a)](LICENSE)

## 先判断你需要哪种同步

| 你的目标 | 使用的模式 |
|---|---|
| 把未提交文件、媒体、依赖和 `.git` 一起搬到新设备 | **完整项目迁移** |
| 只把允许迁移的 Codex 对话和接收状态带过去 | **对话接力** |
| 只同步已经选择并提交的代码历史 | **Git 开发同步** |
| 在新设备恢复备份，同时保留校验和回滚证据 | **事务式恢复** |

**立即使用：**前往 [最新 Release](https://github.com/ranveerankhira-gif/cross-device-project-sync/releases/latest)，下载 `cross-device-project-sync-<version>.zip`，并用 `SHA256SUMS.txt` 核对文件。

## 它解决什么问题

普通 `git push` 不能保存未跟踪文件、媒体、模型、缓存、完整目录结构和本地对话；直接复制整盘又容易混入浏览器登录态、Token、接收账本和其他账号数据。本 Skill 把这些边界写成可执行检查，并区分三种任务：

| 模式 | 保存什么 | 不代表什么 |
|---|---|---|
| 完整项目／整机迁移 | 明确范围内的项目字节、目录结构和允许迁移的 Codex 对话 | 不是实时同步，也不会自动合并分叉 |
| 对话接力 | 用户可见对话、来源身份、增量与接收状态 | 不复制账号认证，不把旧授权带到新设备 |
| Git 开发同步 | 用户明确选择的提交与远端分支 | 不等于完整项目备份 |

## 核心能力

- **完整快照**：不使用 `.gitignore` 缩减完整备份；保留隐藏文件、未提交内容、媒体、依赖、缓存和 `.git`，并验证路径与字节清单。
- **多实例边界**：只扫描配置中明确允许的 Codex 实例；认证文件、浏览器状态、数据库和账号私有账本不得进入传输包。
- **稳定身份**：以 lineage／branch、源设备和源实例区分对话；相同内容去重，真正分叉保留为独立历史，不自动拼接。
- **不可变发布**：完整备份可发布到用户配置的私有 GitHub 仓库，以 Release 绑定、SHA-256 和清单验证来源与资产。
- **事务式恢复**：先在新目录暂存和校验；只有满足所有权、基线与回滚条件时才替换调用账号拥有的已接收副本。
- **失败可审计**：锁、选择回执、计划、队列、接收证明和最终验收均绑定哈希；失败或输入变化不会被伪装成成功。

## 安装

正式版本请从 [GitHub Releases](https://github.com/ranveerankhira-gif/cross-device-project-sync/releases/latest) 下载，并核对同一版本的 `SHA256SUMS.txt`。

1. 下载 `cross-device-project-sync-<version>.zip`。
2. 核对 SHA-256，并阅读 `SKILL.md`、`agents/openai.yaml`、脚本和安全说明。
3. 将完整目录复制到三个并行 Codex 共用的位置：

   ```text
   C:\Users\<你的用户名>\.agents\skills\cross-device-project-sync
   ```

4. 从 `config.example.json` 创建机器私有配置，但把真实仓库、设备名、实例路径与状态根目录留在 Skill 目录外。
5. 重启 Codex 后，通过 `$cross-device-project-sync` 显式调用。

整套仓库下载文件名为 `cross-device-project-sync-v0.1.0.zip`。

## 运行要求

- Windows；
- Python 3.10+；
- PowerShell；
- 仅在使用 Git／GitHub 工作流时需要 Git 与 GitHub CLI `gh`；
- 一个用户自行创建、配置并确认可见性的 GitHub 仓库。完整备份默认要求私有仓库。

本仓库不携带账号、Token、设备注册表或默认可用的云端目的地。

## 安全边界

- `allow_implicit_invocation: false`：不会因为普通“同步”“备份”或文件操作自动介入。
- 盘点和就绪检查可保持只读；上传、下载、恢复、覆盖、删除、修改 Release 或改变可见性都需要当前明确指令。
- 复用本机已登录的官方 `gh`，不读取、导出或写入令牌，也不自动切换账号。
- 示例仓库 `example-owner/example-private-backups` 是不可用占位符；脚本会拒绝把它当成真实目的地。
- 大型快照可能消耗磁盘、带宽和 GitHub 配额；新增付费路径或明显成本前必须停止确认。
- 不自动覆盖不同内容，不把不同设备或分叉历史静默合并，也不因源端消失删除接收端项目或对话。

完整边界见 [`security-and-permissions.md`](skills/cross-device-project-sync/references/security-and-permissions.md)。

## 开发与验证

```powershell
python -X utf8 scripts/build_catalog.py
python -X utf8 scripts/validate_repo.py
python -X utf8 scripts/package_release.py --release all --verify-only
python -X utf8 -m unittest discover -s scripts/tests -v
python -X utf8 -m unittest discover -s skills/cross-device-project-sync/tests -v
python -m compileall -q skills scripts
```

---

如果它帮你避免了一次“代码到了、项目没到”或“文件到了、对话丢了”的迁移事故，欢迎点一个 ⭐，让更多跨设备 Codex 用户找到它。

