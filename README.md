# TG AI 调度台

在手机上通过 Telegram 给电脑发消息，调度台启动对应的 AI CLI（codex / Claude Code）干活，
并把过程与结果实时回传到 Telegram。**一个聊天窗口 = 一段连续会话**：AI 记得之前所有
对话和干过的活，就像在终端里持续用同一个会话工作一样。

本目录是可公开发布的源码版，不包含 Bot token、用户白名单、会话状态、聊天历史、
运行日志或本机路径。项目采用 [MIT License](LICENSE)。

## 架构

```
手机 Telegram ──► @你的机器人 ◄──长轮询── api.telegram.org（走本机代理，无需公网 IP）
                        │
              调度台（本脚本，常驻运行）
              ├─ 鉴权：只响应白名单用户
              ├─ 调度：每个聊天一个串行队列
              └─ Runner：子进程启动 codex / claude，解析 JSONL 事件流
                        │
              AI CLI 在指定工作目录里干活（读写文件、跑命令…）
```

会话续接原理：codex 用 `codex exec resume <会话ID>`、claude 用 `claude -p --resume <会话ID>`。
会话 ID 保存在 `state.json`，重启调度台不丢上下文。

## 首次配置

准备 Python 3.11 或更高版本，并确保 `codex` 和/或 `claude` CLI 已安装、可在终端中运行且已完成登录。

1. **创建机器人**：Telegram 里找 **@BotFather** → 分别为 `My_codex`、`My_claude` 创建机器人并复制两个 token。
2. **获取你的 ID（两种方式任选）**：
   - **配对模式（推荐）**：`.env` 里 `ALLOWED_USER_IDS` 先留空直接启动，用手机给自己的机器人发条
     消息，它会回复你的数字 ID（电脑窗口日志里也会显示），填回 `.env` 重启即完成绑定；
   - 或给 **@userinfobot** 发条消息，记下它回复的数字 ID。
3. **填配置**：复制 `.env.example` 为 `.env`，把 `My_codex` token 填入 `BOT_TOKEN`，把
   `My_claude` token 填入 `CLAUDE_BOT_TOKEN`，并填写 `ALLOWED_USER_IDS`。需要代理时再填写
   `PROXY_URL`，例如 `http://127.0.0.1:7890`。
4. **安装依赖**（已装过可跳过）：
   ```
   pip install -r requirements.txt
   ```
5. **启动**：双击 `start.bat`，或命令行运行 `python main.py`。该窗口会常驻监督两个 Bot，按一次
   `Ctrl+C` 会同时安全关闭 Codex、Claude 及其正在运行的 AI 子进程。启动日志会显示电脑 GUI 地址，默认仅本机可访问。
   调度台运行后也可以直接双击 `open-gui.bat` 打开网页。
6. 在 Telegram 里给你的机器人发 `/start`，然后直接发消息即可。

## 命令

| 命令 | 作用 |
|------|------|
| 直接发文字 | 交给当前会话的 AI 处理（上下文连续） |
| `/sw` | 列出所有会话（👉=当前，🟢=运行中） |
| `/sw 编号` / `/sw 名称` | 按列表编号或完整名称切换会话（正在跑的任务不受影响） |
| `/new 名称` | 新建命名会话并切换；`/new` 不带名字 = 清空当前会话上下文 |
| `/del 编号` / `/del 名称` | 按列表编号或完整名称删除会话 |
| `/stop` | 终止当前会话的任务并清空其队列 |
| `/stopall` | 终止所有会话的任务 |
| `/agent codex` / `/agent claude` | 单 Bot 兼容模式下切换 AI；双 Bot 模式下每个 Bot 的 AI 固定 |
| `/model` / `/model 编号或名称` | 查看或切换当前会话模型；`/model 0` 恢复 `.env`/CLI 默认 |
| `/think` / `/think 编号或名称` | 查看或切换当前会话思考强度；`/think 0` 恢复默认 |
| `/api` / `/api 编号` | 不带参数只读查看；带编号才切换当前 Bot 的 cc-switch API、停止其任务并独立重启 |
| `/cd C:\projects\某项目` | 切换当前会话工作目录（codex 会开新会话） |
| `/status` | 当前会话详情 + 会话列表 |

## 多会话工作流

配置两个 token 后，两个 Bot 完全隔离：`My_codex` 的会话固定运行 Codex，`My_claude` 的会话固定运行
Claude；两边有独立的当前任务、队列和状态文件，但共享历史数据库，并汇总到同一个电脑 GUI。首次启用双 Bot 时，旧状态中
当前选择为 Claude 的会话会自动迁移到 `state.claude.json`，原 `state.json` 会保留一份迁移前备份。
若 `CLAUDE_BOT_TOKEN` 留空，则保持原来的单 Bot 兼容模式，仍可使用 `/agent` 切换。

每个会话拥有独立的 AI 上下文和工作目录，适合「多线程」干活：

```
你: /new 博客项目          ← 新建会话，自动继承工作目录
你: /cd C:\projects\my-blog
你: 排查首页加载慢的问题      ← codex 开始跑（🟢）
你: /sw 毕设                ← 不用等，切到另一个会话
你: 跑一下仿真并把结果整理成表  ← 毕设会话也开工，两个任务并行
📢 [博客项目] codex …        ← 哪个先做完哪个先回传，消息都带会话名
你: /sw 博客项目             ← 回来继续，上下文原样保留
```

- 同一个会话内任务串行排队（避免上下文打架），不同会话之间并行。
- AI 的正文、命令和工具进度会约每 4 秒合并更新到 Telegram；长输出自动分片。任务失败时已发送的过程会保留，错误另行追加。
- Codex 单轮任务不设时长上限；Claude 可用 `.env` 的 `CLAUDE_AUTO_COMPACT_SECONDS` 设置预防性压缩周期（当前为 600 秒）。每个工作段到时后，调度器会中断 Claude 子进程，在同一会话执行 `/compact`，再发送“继续”；设为 `0` 可关闭。`CLAUDE_AUTO_COMPACT_RETRIES` 控制真正压缩失败后的重试次数（当前为 1）。需要人工中止时仍可使用 `/stop`、`/stopall` 或网页“停止任务”。
- 定时压缩是上下文超限的兜底，不按 token 使用量触发；到点时正在执行的工具会被终止，因此写文件或运行测试可能只完成一部分，Claude 续跑后需要自行核对现场。
- 会话和上下文存在 `state.json`，重启调度台、重启电脑都不丢。
- 每个会话的模型与思考强度覆盖也随状态文件持久化；Codex 每回合通过 CLI 参数应用，Claude 通过 `--effort` 应用。
- `/api` 只读取 cc-switch 的供应商数据库且不会重启；只有 `/api 编号` 才会写入并独立重启当前 Bot，另一个 Bot 不受影响。写入 CLI 配置前会备份到项目 `backups/`；官方登录和代理接管状态需回到 cc-switch GUI 切换。

## 电脑 GUI

统一入口位于 `127.0.0.1:8765`，同时显示 Codex 和 Claude 任务。Claude 进程仍在 `GUI_PORT_CLAUDE`（默认 `8766`）提供仅供统一入口使用的本机后端，这保留了两个 Bot 的进程隔离；平时只需使用 `open-gui.bat` 打开 `8765`。首次生成的访问凭据保存在 `.gui-token`，也可以在 `.env` 设置 `GUI_TOKEN` 固定它。

网页可查看、新建、搜索和切换任务，提交消息及停止指定任务。网页与 Telegram 共享任务和执行队列，但各自保存当前选择；网页切换不会改变 Telegram 当前会话。升级后的对话记录保存在 `history.sqlite3`，旧对话因原平台未保存正文而无法补入。

推荐的跨设备工作流：在电脑网页中创建并推进任务；出门后打开对应的 `My_codex` 或 `My_claude`，发送 `/sw` 查看该 Bot 的任务，再用 `/sw 任务名` 切入并继续发送消息。任务目录、队列和 AI 会话 ID 均保持不变。普通终端中单独启动、未登记到调度台的 Claude/Codex 会话不会自动出现。

## 使用示例

```
你: /cd C:\projects\my-blog
你: 看看这个项目结构，把首页加载慢的问题排查一下
🤖 codex 开始处理…（会实时显示正在执行的命令）
✅ codex 结束，用时 3分12秒
    （AI 的回复…）
你: 刚才说的第 2 点直接改了吧
    （codex 带着上面的上下文继续干活）
```

## 安全说明

- **白名单**：机器人只响应 `ALLOWED_USER_IDS` 里的用户。白名单留空时处于「配对模式」：
  只回复来访者的 ID、不执行任何任务；填入 ID 重启后即锁定，陌生人发消息一律静默忽略。
  （Telegram bot token 若泄露，任何人都能冒充你操作 —— 所以白名单必须配置。）
- **token 保管**：`BOT_TOKEN` 泄露时找 @BotFather 发 `/revoke` 可随时吊销重发。
- **本地数据**：`.env`、`.gui-token`、`state*.json`、`history.sqlite3*`、日志和 `backups/`
  都不应提交；详见 [SECURITY.md](SECURITY.md) 与 `.gitignore`。
- **AI 权限**：codex 默认 `workspace-write` 沙箱（可改工作区文件、可联网）；
  claude 默认 `bypassPermissions`（全自动执行，无确认）。想更保守可改 `.env` 里的
  `CODEX_SANDBOX=read-only` 或 `CLAUDE_PERMISSION_MODE=acceptEdits`。
- **前提**：AI 已在本机登录过（codex / claude 各自能正常命令行使用），调度台只是替你发指令。

## 常见问题

- **启动后一直重试连接**：代理没开或 `PROXY_URL` 端口不对。确认 Clash 在运行、端口正确。
- **找不到 codex/claude 命令**：确认终端里能直接运行这两个命令（在 PATH 中）。
- **AI 报未登录**：先在本机终端手动跑一次 `codex` / `claude` 完成登录。
- **claude 没执行命令**：检查 `CLAUDE_PERMISSION_MODE`，`default` 模式下需要确认的操作会被拒绝。

## 后续可扩展

- 图片/文件消息支持（codex `-i` 可直接吃图）
- 多任务并行（按编号管理多个运行中任务）
- Webhook 替代长轮询（需要公网，一般没必要）

## 测试

```powershell
python -m unittest discover -p "test_*.py" -v
node --check gui-prototype/app.js
```

Python 测试验证调度、流式输出、双 Bot 隔离与 Web API；Node 命令只做前端脚本语法检查。

## 开源发布说明

该副本采用源码白名单方式整理，只包含程序、测试、静态资源和说明文件。运行后产生的
状态、消息历史、日志、GUI 凭据和 cc-switch 配置备份均由 `.gitignore` 排除。发布前请再执行：

```powershell
git status --short --ignored
```

确认没有把本机配置或运行数据加入暂存区。若真实 token 曾进入任何待发布目录或 Git 历史，
请先吊销并重新生成，再发布。
