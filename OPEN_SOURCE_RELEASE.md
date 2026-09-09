# 开源副本说明

整理日期：2026-09-09

## 收录范围

- Telegram 双 Bot 调度、会话队列、流式输出与本机 Web API 源码
- Codex / Claude Code Runner 与可选 cc-switch 集成
- 本机 GUI 静态资源、Windows 启动脚本
- 单元测试、依赖清单、示例环境变量和安全说明

## 明确排除

本副本没有复制原运行环境中的以下内容：

- 真实 `.env` 和 GUI 访问 token
- Telegram 用户 ID、任务状态、AI 会话 ID
- SQLite 聊天历史及 WAL/SHM 文件
- 运行日志、配置备份、内部工作元数据和缓存

这些文件的生成路径同时写入了 `.gitignore`。`.env.example` 仅包含占位符和安全默认值；
使用者应复制为 `.env` 后在本机填写自己的凭据。

## 整理时验证

- `python -m unittest discover -p "test_*.py" -v`：43 项通过
- `node --check gui-prototype/app.js`：通过
- `python -m compileall -q .`：通过
- 对原 `.env` 中的 Bot token、用户 ID、GUI token 和本机工作目录做了精确值比对：副本中无匹配
- 检查常见 Telegram、OpenAI、GitHub 和私钥格式：未发现真实凭据

上述结果是源码和本机测试证据，不代表第三方 CLI、Telegram 网络或具体操作系统环境已完成端到端验收。

## 发布者待办

1. 确认 MIT License 符合你的发布意图和所用第三方依赖的许可证要求。
2. 在发布平台新建空仓库，把本目录作为仓库根目录。
3. 提交前检查 `git status --short --ignored`，并再运行一次密钥扫描。
4. 如果真实 token 曾进入任何 Git 历史，先吊销并重新生成，不能只删除工作区文件。
