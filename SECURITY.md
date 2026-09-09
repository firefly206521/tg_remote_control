# 安全说明

## 发布前检查

本项目会在本地生成或读取多类敏感数据。公开仓库中不得包含：

- `.env`、`.gui-token`、`.restart-notice*`
- `state*.json`、`history.sqlite3*`、`*.log`
- `backups/`、`workspace/`、`.zcode/`
- Codex、Claude、cc-switch 或 Telegram 的登录凭据与配置备份

上述路径已加入 `.gitignore`。首次提交前仍建议运行敏感信息扫描，并用
`git status --ignored` 核对忽略结果。已经提交过的密钥不能只靠删除文件解决，
应立即在对应平台吊销并重新生成，同时清理 Git 历史。

## 运行安全

- 务必设置 `ALLOWED_USER_IDS`。白名单为空只适合首次获取自己的 Telegram ID。
- `.env.example` 中的值仅为占位符，不可直接用于生产环境。
- GUI 固定监听 `127.0.0.1`。不要通过端口映射直接暴露到公网。
- `CODEX_SANDBOX` 和 `CLAUDE_PERMISSION_MODE` 决定 AI CLI 的本机权限；先从较保守的
  配置开始，并使用专门的工作目录。
- `/api 编号` 会改写本机 CLI 配置并创建凭据备份。不了解 cc-switch 时不要使用该命令。

## 报告漏洞

请通过项目维护者提供的私密渠道报告安全问题，不要在公开 Issue 中粘贴 token、
用户 ID、会话记录、绝对路径或日志原文。
