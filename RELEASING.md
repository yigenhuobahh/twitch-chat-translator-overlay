# 发布前检查清单

此清单面向维护者；普通用户只需使用 `install.bat` 和 `run.bat`。

## 何时可以发布

只有当候选提交已经通过 CI、关键 Windows 双击流程完成一次人工验收，并且没有未解释的失败或安全告警时，才将 `0.2.4.dev0` 改为正式版本并创建标签。不要为了赶宣传时间跳过这一步。

## 候选提交验收

1. 确认工作区干净，并检查 `CHANGELOG.md` 的 Unreleased 内容已写清用户可见变化和已知限制。
2. 在干净虚拟环境安装 wheel 或 sdist，运行 `twitch-chat-overlay --doctor`，确认安装包本身而非源码目录可用。
3. 运行 `python scripts/run_tests.py --max --coverage`，并确认 Ruff、完整测试、覆盖率门槛和打包烟测均通过。
4. 在 Windows 上从资源管理器双击 `install.bat` 和 `run.bat`，然后在终端运行 `run_cli.bat doctor`：完成一次离线演示和一次短原文预览。确认结果目录、取消和诊断导出都可用。
5. 查看 GitHub Actions 的 Windows、Linux、macOS 与安全检查；只接受全部相关任务为绿色的候选提交。
6. 若本次涉及下载、翻译或渲染，额外完成相应的真实素材验收，并确认 OAuth、API Key、聊天内容没有进入历史、结果清单或诊断。

## 创建发布

1. 将 `pyproject.toml` 的版本从开发版本改为正式版本，例如 `0.2.4`，并将变更记录移入带日期的版本标题。
2. 再次执行完整发布验收，提交并推送 `main`，等待该提交的 CI 通过。
3. 创建完全匹配的标签，例如 `v0.2.4` 并推送。Release 工作流会校验标签与包版本一致、执行发布门槛、构建 wheel/sdist、生成 SHA256SUMS，并创建 GitHub Release。
4. 下载 Release 中的 wheel 与 sdist，分别在全新虚拟环境安装并运行 `--doctor`；检查 Release 页面同时包含两个包和校验文件。
5. 发布后立即将版本提升到下一个 `.dev0` 周期，避免后续未发布提交继续显示旧稳定版号。

## 发现问题时

停止标签创建或删除尚未公开的错误标签；不要覆盖已发布标签。修复后从新的候选提交重新验收。若用户已下载受影响版本，应在 Release 说明和 `CHANGELOG.md` 中明确受影响范围与升级方式。
