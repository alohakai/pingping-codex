# Feishu Codex

将一个飞书企业自建应用机器人连接到本机 Codex。每个飞书会话拥有独立、持久化的 Master thread；Master 可以启动后台 subagent/worker，继续与用户即时聊天，并在 worker 完成后主动汇报。

## 架构

```text
飞书用户
  → 飞书应用机器人（长连接）
  → Feishu Codex 网关
  → Codex App Server
  → Master thread
      ↳ worker thread
      ↳ worker thread
```

网关使用 SQLite 保存飞书会话、Master thread 和 worker 任务的对应关系。Codex 对话本身由本机 Codex App Server 持久化。

普通文本消息到达后，机器人会先随机回复一个带表情的“正在处理”回执，再把
最终回答发回飞书。明确以“修改、补充、改成、不要”等词开头的需求变更，会
即时 steer 当前 Master turn；当会话中只有一个运行中的 worker 时，也会同步
steer 该 worker。

## 前置条件

- macOS 上已经安装并登录 Codex/ChatGPT 桌面应用。
- Python 3.11 或更高版本。
- 一个飞书企业自建应用。

## 飞书后台配置

1. 在[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用。
2. 添加“机器人”应用能力。
3. 至少开通以下权限：
   - 读取用户发给机器人的单聊消息。
   - 接收群聊中 @机器人的消息。
   - 获取与发送单聊、群组消息：`im:message`。
4. 在“事件与回调”中选择“使用长连接接收事件”。
5. 添加事件 `im.message.receive_v1`。
6. 创建并发布一个应用版本，将机器人加入需要使用的群聊或可用范围。

长连接不需要公网 IP、域名或内网穿透。

## 安装

项目已经创建好本地虚拟环境。如果需要重新安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
```

复制环境变量模板：

```bash
cp .env.example .env
```

编辑 `.env`，填写：

```dotenv
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
```

不要将 `.env` 或真实 App Secret 提交到 Git。

本地部署可以绑定飞书应用“小萍萍”；出于安全原因，App Secret 必须由应用
所有者在本机 `.env` 的 `FEISHU_APP_SECRET` 中填写。

## 配置允许访问的项目

编辑 `config/projects.json`：

```json
{
  "gateway": {
    "path": "/absolute/path/to/project",
    "description": "项目说明",
    "sandbox": "workspace-write"
  }
}
```

飞书用户只能让 Codex 操作这里列出的项目。不要把主目录或磁盘根目录加入列表。

## 验证与启动

不连接飞书，仅检查 Codex、配置和 SQLite：

```bash
.venv/bin/python -m feishu_codex --check
```

启动机器人：

```bash
zsh scripts/run.sh
```

本地模拟一条飞书消息：

```bash
.venv/bin/python -m feishu_codex --simulate "介绍一下你自己，不要启动 worker"
```

模拟命令会真实调用 Codex，但不会连接飞书。

验证 worker 启动、完成和 Master 自动汇报：

```bash
.venv/bin/python -m feishu_codex \
  --simulate "启动一个后台 subagent，让它返回 WORKER_OK" \
  --simulate-wait-workers 90
```

## 飞书命令

```text
/任务
/状态 task_xxxxxxxx
/补充 task_xxxxxxxx 补充说明
/取消 task_xxxxxxxx
/项目
/新对话
/批准 approval_xxxxxxxx
/拒绝 approval_xxxxxxxx
/帮助
```

`/新对话` 只解除当前飞书会话与 Master 的绑定，不会删除历史 Codex thread，也不会删除正在运行的 worker。

## Session 与 memory

- `data/state.db` 保存飞书 `chat_id` 到 Codex Master thread ID 的映射、事件去重
  和 worker 状态；该文件不提交 Git。
- Codex 的完整对话记录由本机 Codex 保存到
  `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`，并由
  `~/.codex/session_index.jsonl` 和 Codex 自身状态数据库建立索引。
- `memory/chats/<chat-hash>/` 是每个飞书会话独立的长期记忆目录。
- `memory/shared/` 用于人工维护并允许提交的公共知识；`memory/shared/private/`
  只留在本机。

若需求在回答生成过程中发生变化，直接发送“修改：……”“补充：……”或
“改成……”即可。网关会尝试即时更新当前回答及唯一运行中的 worker；有多个
worker 时，使用 `/补充 task_xxx 内容` 可明确指定目标。

## 审批

默认 `CODEX_APPROVAL_POLICY=on-request`。当 Codex 请求需要用户确认的命令或文件操作时，机器人会发送：

```text
批准：/批准 approval_xxxxxxxx
拒绝：/拒绝 approval_xxxxxxxx
```

审批由网关直接处理，不需要等待 Master 开启新一轮对话。

## 常驻运行

`deploy/com.feishu-codex.plist.example` 是 macOS `launchd` 模板。填写 `.env` 并完成前台验证后，再复制到 `~/Library/LaunchAgents/` 并加载。

正式运行前建议在 `.env` 中配置 `FEISHU_ALLOWED_USERS` 或 `FEISHU_ALLOWED_CHATS` 白名单。

## 当前边界

- 第一版只处理文本消息。
- 飞书消息按会话串行进入 Master，worker 可以并行运行。
- worker 完成后由网关唤醒 Master，再由 Master 生成面向用户的汇报。
- 不会自动批准危险操作。
- Mac、Codex App Server 和机器人进程必须保持在线。
- 飞书 SDK 日志固定不低于 WARNING，避免把长连接的临时鉴权参数写入日志。
- `CODEX_MEMORY_DIR` 默认为项目中的 `memory/`，会话记忆按 chat hash 隔离。
