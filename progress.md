# 工作日志

## 2026-07-28：飞书接入 Codex 本地端方案调研

- 阅读 Codex 官方手册中 MCP、插件、`codex exec`、App Server 等扩展与自动化能力。
- 阅读本地飞书技能说明，确认飞书文档操作的用户身份授权、Bot 身份限制和最小权限原则。
- 核对飞书开放平台的应用机器人、消息事件订阅、长连接和自定义机器人能力。
- 确认本机 Codex CLI 已安装，版本为 `codex-cli 0.144.2`，支持 `codex mcp` 和 `codex exec --json`。
- 确认当前 shell 尚未安装 `lark-cli`，若选择“Codex 读取/编辑飞书内容”路线，需要先安装并初始化认证。
- 形成三类接入建议：
  - Codex 调用飞书：使用 `lark-cli`/飞书技能，或封装为本地 STDIO MCP。
  - 飞书对话驱动 Codex：企业自建应用机器人通过长连接接收消息，本地桥接程序异步调用 `codex exec`，再由飞书消息 API 回传。
  - 单向通知：群自定义机器人 Webhook，仅适合 Codex 向群内推送结果，不支持交互式对话。

## 2026-07-28：按“Codex 接入飞书智能体（Aily）”方向补充调研

- 根据用户澄清，将目标修正为：飞书智能体作为调用方，使用 Codex 的本地代码代理能力。
- 核对 Aily 当前能力：新版自定义智能体支持接入业务系统 MCP；工作流也可通过 HTTP 自定义连接器调用外部系统。
- 核对本机 Codex 接口：
  - `codex mcp-server` 仅提供 STDIO 传输，无法被运行在飞书云端的 Aily 直接启动或访问。
  - `codex app-server` 支持 STDIO、Unix Socket 和 WebSocket，但不应直接暴露到公网给 Aily。
  - `codex exec --json` 适合作为受控网关内部的任务执行入口。
- 推荐架构：Aily MCP/自定义连接器 → 带鉴权的公网 HTTPS 网关 → 本机任务队列 → `codex exec` → 查询任务状态/结果。
- 推荐将耗时任务设计为异步接口：提交任务、查询状态、获取结果；同时限制用户、工作目录、命令权限和并发数。

## 2026-07-28：收敛为普通飞书机器人接入

- 根据用户最新要求，移除 Aily、Aily MCP 和自定义连接器方案。
- 最终推荐架构：飞书企业自建应用机器人 → 飞书 SDK 长连接 → 本机桥接程序 → `codex exec --json` → 飞书消息 API 回复。
- 该方案不需要公网 IP、域名或内网穿透，但本机需要保持在线并运行桥接程序。
- 明确不能使用群自定义 Webhook 机器人替代：Webhook 机器人只能向群内推送通知，不能接收用户消息或完成双向对话。

## 2026-07-29：OpenClaw 式 master/worker 对话架构

- 将一次性 `codex exec` 方案升级为常驻 `codex app-server` 方案，以获得持久线程、流式事件、恢复、分叉和中断能力。
- 设计一个飞书会话对应一个 Codex master thread；使用 SQLite 保存 `feishu_chat_id → master_thread_id` 映射。
- master thread 只负责即时聊天、需求澄清、任务路由和结果汇总，不直接承担长耗时工作。
- master 通过 Codex 多智能体能力启动 worker/subagent threads；桥接服务监听 `collabToolCall`、`turn/completed` 等事件，登记 worker 任务并在完成后唤醒 master。
- worker 完成后，由桥接服务向 master 注入内部完成事件，master 结合原始对话生成面向用户的飞书回复。
- 规划飞书命令：查看任务、取消任务、新建上下文、查看 worker；普通消息进入 master，紧急补充可使用 turn steer，中止使用 turn interrupt。
- 规划安全边界：会话和用户白名单、固定工作目录、幂等去重、任务并发与超时、危险操作卡片审批、敏感信息过滤。

## 2026-07-29：完成 Feishu Codex 第一版实现

- 创建 Python 项目和本地虚拟环境，安装 `lark-oapi 1.7.1`，使用 Codex 桌面应用内置 CLI。
- 新增核心文件：
  - `src/feishu_codex/app_server.py`：Codex App Server JSON-RPC/JSONL 客户端。
  - `src/feishu_codex/feishu.py`：飞书长连接收消息和消息回复。
  - `src/feishu_codex/orchestrator.py`：Master 队列、worker 跟踪、完成回流、任务命令和审批。
  - `src/feishu_codex/store.py`：SQLite 会话、事件去重和任务状态。
  - `src/feishu_codex/config.py`：环境变量、安全配置和项目白名单。
  - `src/feishu_codex/__main__.py`：检查、模拟和正式运行入口。
- 新增 `AGENTS.md`，固化 Master 只负责即时沟通和编排、长任务必须委派、安全操作需确认等规则。
- 新增 `config/projects.json`，默认只允许访问当前网关项目。
- 新增飞书命令：任务列表、状态、补充、取消、项目列表、新对话、批准、拒绝和帮助。
- 新增文字审批链路：Codex 的命令/文件审批请求会转为 `/批准 approval_xxx` 或 `/拒绝 approval_xxx`。
- 新增 `scripts/run.sh` 和 `deploy/com.feishu-codex.plist.example`，为前台运行和 macOS 常驻运行提供入口。
- 新增 `README.md`、`.env.example`、`.gitignore` 和完整配置说明。
- 新增 8 个单元测试，覆盖配置、事件去重、会话/任务状态、飞书消息解析和 `subAgentActivity` worker 登记。
- 验证结果：
  - Ruff 静态检查和格式检查通过。
  - 8 个单元测试全部通过。
  - Codex App Server 初始化和 `model/list` 真实握手通过，当前识别模型为 `gpt-5.6-sol`。
  - Master 单轮模拟通过，正确返回“飞书网关测试通过”。
  - Master/worker 全链路模拟通过：捕获 subagent thread、登记 `task_…`、worker 返回 `WORKER_OK`、自动唤醒 Master 汇报。
- 飞书外部配置进度：
  - 已打开飞书开发者后台。
  - 当前停在飞书移动端扫码登录，等待用户扫码后继续创建企业自建应用、开通机器人权限和配置长连接事件。

## 2026-07-29：绑定现有飞书应用“小萍萍”

- 用户完成飞书开发者后台登录，并指定复用现有企业自建应用“小萍萍”，不再创建新应用。
- 核对“小萍萍”应用配置：
  - 应用已启用，机器人能力已经添加。
  - `im:message`（获取与发送单聊、群组消息）已开通。
  - `im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）已开通。
  - `im:message.group_at_msg:readonly`（获取群组中用户 @ 机器人的消息）已开通。
  - 事件订阅方式已经是官方 SDK 长连接。
  - `im.message.receive_v1` 接收消息事件已经添加。
- 新增本地 `.env`，写入“小萍萍”的非敏感 App ID，并保留空的
  `FEISHU_APP_SECRET`，由应用所有者在本机安全填写。
- 更新 `README.md`：
  - 将当前飞书后台展示的发送权限名称修正为 `im:message`。
  - 记录当前绑定应用以及 App Secret 的安全填写要求。
- 未读取、展示或记录 App Secret。
- 复检时发现当前 Python 运行时会跳过以 `__editable__` 开头的隐藏
  `.pth` 文件，导致 `pip install -e .` 的可编辑安装无法导入包；已改为
  普通 wheel 安装，并同步更新 README 的安装命令。
- 修复安装后重新验证：
  - 8 个单元测试全部通过。
  - Ruff 静态检查通过。
  - Ruff 格式检查通过。
  - `.env` 已由 `.gitignore` 排除。

## 2026-07-29：凭证验证与首次长连接

- 用户在本机 `.env` 填写 App Secret 后，完成不回显的凭证存在性检查。
- `--check` 验证通过：
  - Codex App Server 初始化成功。
  - 模型列表可用，识别模型为 `gpt-5.6-sol`。
  - 项目、Master 目录和 SQLite 状态库配置有效。
- 首次启动“小萍萍”后，飞书官方 SDK 长连接建立成功。
- 安全复检发现 lark-oapi 的 INFO 日志会输出包含临时连接鉴权参数的
  WebSocket 地址；已停止首次进程，修改 `feishu.py`，强制飞书 SDK
  的日志级别不低于 WARNING，避免后续把临时 access_key/ticket 写入日志。
- 新增单元测试，覆盖 INFO/DEBUG 均被收紧至 WARNING、ERROR 保持 ERROR。
- 重新构建并安装 wheel 后，9 个单元测试、Ruff 静态检查和格式检查全部通过。
- 使用脱敏后的日志配置重新启动网关：
  - Codex App Server 初始化成功。
  - 飞书长连接线程已启动。
  - 观察期内未再输出长连接 URL、access_key 或 ticket。
  - 当前网关进程保持运行，等待飞书端真实消息联调。

## 2026-07-29：即时回执、实时需求变更与 memory 空间

- 已确认飞书真实会话创建了独立 Master thread；网关映射保存在
  `data/state.db`，对应 Codex rollout 保存在本机
  `~/.codex/sessions/2026/07/29/`。
- 普通文本消息现在会先随机回复一个带表情的“正在处理”回执，回执失败不会
  阻塞正式任务。
- 新增需求变更识别：以“修改、补充、改成、改为、不要、调整”等词开头的
  消息会被视为实时修改。
- 若 Master 正在生成回答，需求变更会通过 `turn/steer` 即时写入当前 turn。
- 若会话中恰好有一个运行中的 worker，需求变更也会直接 steer 该 worker；
  同时写入 Master 上下文，避免最终汇总丢失变更。
- 多 worker 并行时不盲目选择目标，仍可使用 `/补充 task_xxx 内容` 精确指定。
- 新增 `memory/` 持久记忆空间：
  - `memory/chats/<chat-hash>/`：按飞书会话隔离，本机保存且不提交。
  - `memory/workers/`：预留 worker 记忆，本机保存且不提交。
  - `memory/shared/`：可提交的公共知识。
  - `memory/shared/private/`：本机私有共享记忆。
- 新增 `CODEX_MEMORY_DIR` 配置；Master 启动/恢复时会获得当前会话的记忆目录，
  并被明确禁止向记忆文件写入密钥、令牌或密码。
- 新增 5 项相关测试，完整测试数增加到 13；Ruff、格式检查和全部测试通过。
- 重新构建 wheel 后，Codex App Server 配置检查通过。
