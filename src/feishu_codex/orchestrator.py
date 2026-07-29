from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from secrets import choice
from typing import Any

from .app_server import DEFERRED, AppServerClient, AppServerError
from .config import Settings
from .feishu import FeishuBot, IncomingMessage
from .store import StateStore, TaskRecord

log = logging.getLogger(__name__)

ACK_MESSAGES = (
    "🤔 正在思考…",
    "🧠 收到，正在处理…",
    "🫡 收到，我来处理…",
    "⚡ 已收到，马上回答…",
    "🔎 正在查看…",
)
UPDATE_ACK_MESSAGES = (
    "🔄 收到新要求，正在调整…",
    "✍️ 已收到修改，马上同步…",
    "🧩 收到补充，正在更新…",
)
REQUIREMENT_UPDATE_PREFIXES = (
    "补充",
    "修改",
    "改成",
    "改为",
    "更改",
    "调整",
    "不要",
    "别再",
    "请改",
    "新增",
    "增加",
    "再加",
    "去掉",
    "删掉",
    "删除",
    "刚才",
    "等等",
    "需求变更",
    "还有",
    "update ",
    "change ",
    "actually ",
    "instead ",
)


@dataclass(frozen=True)
class QueuedTurn:
    chat_id: str
    text: str
    message_id: str | None
    client_message_id: str
    internal: bool = False


@dataclass
class PendingApproval:
    token: str
    rpc_id: int | str
    method: str
    chat_id: str
    thread_id: str
    summary: str


class Orchestrator:
    def __init__(
        self, settings: Settings, store: StateStore, app: AppServerClient
    ) -> None:
        self.settings = settings
        self.store = store
        self.app = app
        self.feishu: FeishuBot | None = None
        self._queues: dict[str, asyncio.Queue[QueuedTurn]] = {}
        self._queue_tasks: dict[str, asyncio.Task[None]] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._loaded_threads: set[str] = set()
        self._turn_messages: dict[tuple[str, str], str] = {}
        self._turn_done: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}
        self._completed_turns: dict[tuple[str, str], dict[str, Any]] = {}
        self._orphan_worker_completions: dict[str, tuple[str, str]] = {}
        self._pending_approvals: dict[str, PendingApproval] = {}
        self._active_master_turns: dict[str, tuple[str, str]] = {}
        self.app.add_notification_handler(self.on_notification)
        self.app.set_server_request_handler(self.on_server_request)

    def attach_feishu(self, feishu: FeishuBot) -> None:
        self.feishu = feishu

    async def handle_message(self, message: IncomingMessage) -> None:
        if not self.store.mark_event_once(message.event_id):
            log.info("忽略重复飞书事件：%s", message.event_id)
            return
        if message.message_type != "text":
            await self._reply(
                message.message_id,
                message.chat_id,
                "当前版本只支持文本消息。文件和图片接入会在后续版本补充。",
            )
            return
        if not message.text:
            await self._reply(message.message_id, message.chat_id, "请发送具体内容。")
            return
        if message.text.startswith("/"):
            handled = await self._handle_command(message)
            if handled:
                return
        is_update = _looks_like_requirement_update(message.text)
        await self._send_ack(message, is_update=is_update)
        master_steered, worker = (
            await self._route_live_update(message) if is_update else (False, None)
        )
        user_text = (
            f"[飞书用户消息，chat_type={message.chat_type}]\n{message.text.strip()}"
        )
        if master_steered:
            return
        if worker is not None:
            user_text = (
                "[飞书用户即时修改需求；网关已直接同步给正在运行的 worker "
                f"{worker.task_id}，不要重复转发或新建 worker]\n"
                f"{user_text}"
            )
        await self.enqueue_turn(
            QueuedTurn(
                chat_id=message.chat_id,
                text=user_text,
                message_id=message.message_id,
                client_message_id=message.event_id,
            )
        )

    async def enqueue_turn(self, turn: QueuedTurn) -> None:
        queue = self._queues.setdefault(turn.chat_id, asyncio.Queue())
        await queue.put(turn)
        task = self._queue_tasks.get(turn.chat_id)
        if task is None or task.done():
            self._queue_tasks[turn.chat_id] = asyncio.create_task(
                self._drain_chat_queue(turn.chat_id),
                name=f"master-queue-{turn.chat_id}",
            )

    async def _drain_chat_queue(self, chat_id: str) -> None:
        queue = self._queues[chat_id]
        while not queue.empty():
            queued = await queue.get()
            try:
                master_thread = await self._ensure_master(chat_id)
                result = await self._run_turn(
                    chat_id,
                    master_thread,
                    queued.text,
                    queued.client_message_id,
                )
                if queued.message_id:
                    await self._reply(queued.message_id, chat_id, result)
                else:
                    await self._send(chat_id, result)
            except Exception as exc:
                log.exception("Master 对话执行失败：chat=%s", chat_id)
                text = f"Codex 对话执行失败：{exc}"
                if queued.message_id:
                    await self._reply(queued.message_id, chat_id, text)
                else:
                    await self._send(chat_id, text)
            finally:
                queue.task_done()

    async def _ensure_master(self, chat_id: str) -> str:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            binding = self.store.get_chat(chat_id)
            if binding and binding.master_thread_id in self._loaded_threads:
                return binding.master_thread_id
            if binding:
                try:
                    await self.app.request(
                        "thread/resume",
                        self._thread_params(chat_id, binding.master_thread_id),
                    )
                    self._loaded_threads.add(binding.master_thread_id)
                    return binding.master_thread_id
                except AppServerError:
                    log.warning(
                        "无法恢复 Master thread，将新建：%s",
                        binding.master_thread_id,
                        exc_info=True,
                    )

            response = await self.app.request(
                "thread/start", self._thread_params(chat_id)
            )
            thread_id = str(response["thread"]["id"])
            self.store.bind_chat(chat_id, thread_id)
            self._loaded_threads.add(thread_id)
            try:
                await self.app.request(
                    "thread/name/set",
                    {"threadId": thread_id, "name": f"飞书 Master {chat_id[-8:]}"},
                )
            except AppServerError:
                log.debug("设置 Master thread 名称失败", exc_info=True)
            return thread_id

    def _thread_params(
        self, chat_id: str, thread_id: str | None = None
    ) -> dict[str, Any]:
        memory_dir = self._chat_memory_dir(chat_id)
        params: dict[str, Any] = {
            "cwd": str(self.settings.master_cwd),
            "sandbox": self.settings.codex_sandbox,
            "approvalPolicy": self.settings.codex_approval_policy,
            "runtimeWorkspaceRoots": self.settings.workspace_roots,
            "developerInstructions": self._runtime_instructions(memory_dir),
        }
        if self.settings.codex_model:
            params["model"] = self.settings.codex_model
        if thread_id:
            params["threadId"] = thread_id
        return params

    def _chat_memory_dir(self, chat_id: str) -> str:
        chat_key = hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:16]
        path = self.settings.memory_dir / "chats" / chat_key
        path.mkdir(parents=True, exist_ok=True)
        return str(path.resolve())

    def _runtime_instructions(self, memory_dir: str) -> str:
        projects = "\n".join(
            f"- {project.alias}: {project.path} ({project.description})"
            for project in self.settings.projects.values()
        )
        return (
            "这是飞书机器人 Master 对话。保持即时响应；长耗时或需要操作文件的"
            "工作必须启动后台 subagent，启动后立即回复用户，不要等待。\n"
            "允许使用的项目如下，禁止访问未列出的路径：\n"
            f"{projects}\n"
            f"当前飞书会话的持久记忆目录：{memory_dir}\n"
            "需要跨轮次长期保留的偏好、约定和项目背景可写入该目录；禁止写入"
            "App Secret、访问令牌、密码等敏感信息。\n"
            "内部 worker 完成事件只用于向用户汇总，不要为汇总任务再启动 worker。\n"
            "当消息标记为“网关已直接同步给正在运行的 worker”时，只需记录需求"
            "变化并向用户确认，不要重复转发或启动新的 worker。"
        )

    async def _run_turn(
        self, chat_id: str, thread_id: str, text: str, client_message_id: str
    ) -> str:
        response = await self.app.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "clientUserMessageId": client_message_id,
                "responsesapiClientMetadata": {"channel": "feishu"},
            },
        )
        turn_id = str(response["turn"]["id"])
        self._active_master_turns[chat_id] = (thread_id, turn_id)
        try:
            completed = await self._wait_for_turn(thread_id, turn_id)
        finally:
            if self._active_master_turns.get(chat_id) == (thread_id, turn_id):
                self._active_master_turns.pop(chat_id, None)
        message = self._turn_messages.pop((thread_id, turn_id), "").strip()
        turn = completed.get("turn") or {}
        status = str(turn.get("status") or "unknown")
        if message:
            return message
        if status == "interrupted":
            return "任务已中止。"
        return f"Codex 本轮已结束，但没有返回文本结果（状态：{status}）。"

    async def _send_ack(self, message: IncomingMessage, *, is_update: bool) -> None:
        try:
            await self._reply(
                message.message_id,
                message.chat_id,
                _random_ack(is_update=is_update),
            )
        except Exception:
            # 表情回执只是体验优化，失败时不能阻塞真实任务。
            log.warning("发送即时表情回执失败", exc_info=True)

    async def _route_live_update(
        self, message: IncomingMessage
    ) -> tuple[bool, TaskRecord | None]:
        """把明确的需求变更即时同步给当前 Master turn 和唯一运行中的 worker。"""
        text = f"[飞书用户即时修改需求]\n{message.text.strip()}"
        master_steered = False
        active_master = self._active_master_turns.get(message.chat_id)
        if active_master:
            thread_id, turn_id = active_master
            try:
                await self.app.request(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "expectedTurnId": turn_id,
                        "input": [{"type": "text", "text": text}],
                        "clientUserMessageId": message.event_id,
                    },
                )
                master_steered = True
            except Exception:
                log.warning("即时修改未能 steer 当前 Master，回退到排队", exc_info=True)

        running = [
            task
            for task in self.store.list_tasks(message.chat_id, limit=100)
            if task.status == "running" and task.turn_id
        ]
        worker: TaskRecord | None = None
        if len(running) == 1:
            candidate = running[0]
            try:
                await self.app.request(
                    "turn/steer",
                    {
                        "threadId": candidate.worker_thread_id,
                        "expectedTurnId": candidate.turn_id,
                        "input": [{"type": "text", "text": text}],
                        "clientUserMessageId": message.event_id,
                    },
                )
                worker = candidate
            except Exception:
                log.warning(
                    "即时修改未能 steer worker：%s",
                    candidate.task_id,
                    exc_info=True,
                )
        elif len(running) > 1:
            log.info(
                "会话中有多个运行中的 worker，需求变更仅交给 Master 判断：chat=%s",
                message.chat_id,
            )
        return master_steered, worker

    async def _wait_for_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        key = (thread_id, turn_id)
        completed = self._completed_turns.pop(key, None)
        if completed is not None:
            return completed
        future = self._turn_done.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._turn_done[key] = future
        try:
            return await future
        finally:
            self._turn_done.pop(key, None)
            self._completed_turns.pop(key, None)

    async def on_notification(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if method == "item/completed":
            await self._on_item_completed(params)
        elif method == "turn/started":
            self._on_turn_started(params)
        elif method == "turn/completed":
            await self._on_turn_completed(params)

    async def _on_item_completed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", ""))
        turn_id = str(params.get("turnId", ""))
        item = params.get("item") or {}
        item_type = item.get("type")
        if item_type == "agentMessage":
            self._turn_messages[(thread_id, turn_id)] = str(item.get("text", ""))
        elif item_type == "collabAgentToolCall":
            await self._on_collab_item(item)
        elif item_type == "subAgentActivity" and item.get("kind") == "started":
            worker_thread = str(item.get("agentThreadId", ""))
            if worker_thread:
                await self._register_worker(
                    sender_thread=thread_id,
                    worker_thread=worker_thread,
                    prompt=str(item.get("agentPath") or "后台任务"),
                )

    async def _on_collab_item(self, item: dict[str, Any]) -> None:
        if item.get("tool") != "spawnAgent" or item.get("status") != "completed":
            return
        sender_thread = str(item.get("senderThreadId", ""))
        prompt = str(item.get("prompt") or "后台任务")
        for worker_thread in item.get("receiverThreadIds") or []:
            await self._register_worker(
                sender_thread=sender_thread,
                worker_thread=str(worker_thread),
                prompt=prompt,
            )

    async def _register_worker(
        self, *, sender_thread: str, worker_thread: str, prompt: str
    ) -> None:
        binding = self.store.get_chat_by_master(sender_thread)
        parent_task = None if binding else self.store.get_task_by_worker(sender_thread)
        if binding:
            chat_id = binding.chat_id
            master_thread = binding.master_thread_id
        elif parent_task:
            chat_id = parent_task.chat_id
            master_thread = parent_task.master_thread_id
        else:
            log.warning("无法为 worker 找到飞书会话：sender=%s", sender_thread)
            return
        task = self.store.create_task(
            chat_id=chat_id,
            master_thread_id=master_thread,
            worker_thread_id=worker_thread,
            prompt=prompt,
        )
        log.info(
            "登记 worker：%s thread=%s master=%s",
            task.task_id,
            task.worker_thread_id,
            master_thread,
        )
        orphan = self._orphan_worker_completions.pop(task.worker_thread_id, None)
        if orphan:
            status, result = orphan
            await self._finish_worker(task, status, result)

    def _on_turn_started(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", ""))
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id", ""))
        task = self.store.get_task_by_worker(thread_id)
        if task and turn_id:
            self.store.update_task(task.task_id, status="running", turn_id=turn_id)

    async def _on_turn_completed(self, params: dict[str, Any]) -> None:
        thread_id = str(params.get("threadId", ""))
        turn = params.get("turn") or {}
        turn_id = str(turn.get("id", ""))
        key = (thread_id, turn_id)
        future = self._turn_done.get(key)
        if future is not None and not future.done():
            future.set_result(params)
        elif self.store.get_chat_by_master(thread_id):
            self._completed_turns[key] = params

        task = self.store.get_task_by_worker(thread_id)
        if task:
            raw_status = str(turn.get("status") or "completed")
            status = {
                "completed": "completed",
                "interrupted": "interrupted",
                "failed": "failed",
            }.get(raw_status, raw_status)
            result = self._turn_messages.get(key, "")
            await self._finish_worker(task, status, result)
            self._turn_messages.pop(key, None)
        elif not self.store.get_chat_by_master(thread_id):
            result = self._turn_messages.get(key, "")
            self._orphan_worker_completions[thread_id] = (
                str(turn.get("status") or "completed"),
                result,
            )

    async def _finish_worker(self, task: TaskRecord, status: str, result: str) -> None:
        if task.status in {"completed", "failed", "interrupted", "cancelled"}:
            return
        updated = self.store.update_task(
            task.task_id, status=status, result=result.strip()
        )
        assert updated is not None
        internal_text = (
            "[内部 worker 完成事件；不要重新委派这个汇总任务]\n"
            f"任务 ID：{updated.task_id}\n"
            f"任务名称：{updated.title}\n"
            f"状态：{updated.status}\n"
            f"Worker 结果：\n{updated.result or 'Worker 没有返回文本结果。'}\n\n"
            "请结合此前飞书对话，主动向用户汇报结果。"
        )
        await self.enqueue_turn(
            QueuedTurn(
                chat_id=updated.chat_id,
                text=internal_text,
                message_id=None,
                client_message_id=f"worker-{updated.task_id}-{uuid.uuid4().hex[:8]}",
                internal=True,
            )
        )

    async def on_server_request(self, message: dict[str, Any]) -> object:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        thread_id = str(params.get("threadId") or params.get("conversationId") or "")
        chat_id = self._chat_for_thread(thread_id)
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            log.warning("拒绝尚未支持的 App Server 反向请求：%s", method)
            return _decline_result(method)
        if not chat_id:
            log.warning("无法路由审批请求，自动拒绝：thread=%s", thread_id)
            return _decline_result(method)

        token = f"approval_{uuid.uuid4().hex[:8]}"
        summary = _approval_summary(method, params)
        self._pending_approvals[token] = PendingApproval(
            token=token,
            rpc_id=message["id"],
            method=method,
            chat_id=chat_id,
            thread_id=thread_id,
            summary=summary,
        )
        await self._send(
            chat_id,
            "Codex 请求执行需要确认：\n"
            f"{summary}\n\n"
            f"批准：/批准 {token}\n"
            f"拒绝：/拒绝 {token}",
        )
        return DEFERRED

    def _chat_for_thread(self, thread_id: str) -> str | None:
        binding = self.store.get_chat_by_master(thread_id)
        if binding:
            return binding.chat_id
        task = self.store.get_task_by_worker(thread_id)
        return task.chat_id if task else None

    async def _handle_command(self, message: IncomingMessage) -> bool:
        command, _, rest = message.text.strip().partition(" ")
        command = command.lower()
        rest = rest.strip()
        if command in {"/帮助", "/help"}:
            await self._reply(message.message_id, message.chat_id, _help_text())
            return True
        if command in {"/任务", "/tasks"}:
            await self._reply(
                message.message_id,
                message.chat_id,
                _format_tasks(self.store.list_tasks(message.chat_id)),
            )
            return True
        if command in {"/状态", "/status"}:
            task = self.store.get_task(rest)
            text = (
                _format_task(task)
                if task and task.chat_id == message.chat_id
                else "没有找到该任务。"
            )
            await self._reply(message.message_id, message.chat_id, text)
            return True
        if command in {"/取消", "/cancel"}:
            await self._cancel_task(message, rest)
            return True
        if command in {"/补充", "/steer"}:
            await self._steer_task(message, rest)
            return True
        if command in {"/新对话", "/new"}:
            old_thread = self.store.reset_chat(message.chat_id)
            if old_thread:
                self._loaded_threads.discard(old_thread)
            await self._reply(
                message.message_id,
                message.chat_id,
                "已结束当前 Master 上下文。下一条普通消息会创建新的对话；已有 worker 不会被删除。",
            )
            return True
        if command in {"/项目", "/projects"}:
            lines = ["允许访问的项目："]
            for project in self.settings.projects.values():
                lines.append(
                    f"- {project.alias}：{project.description}（{project.sandbox}）"
                )
            await self._reply(message.message_id, message.chat_id, "\n".join(lines))
            return True
        if command in {"/批准", "/approve"}:
            await self._resolve_approval(message, rest, approve=True)
            return True
        if command in {"/拒绝", "/deny"}:
            await self._resolve_approval(message, rest, approve=False)
            return True
        return False

    async def _cancel_task(self, message: IncomingMessage, task_id: str) -> None:
        task = self.store.get_task(task_id)
        if not task or task.chat_id != message.chat_id:
            await self._reply(message.message_id, message.chat_id, "没有找到该任务。")
            return
        if task.status not in {"pending", "running", "cancelling"}:
            await self._reply(
                message.message_id,
                message.chat_id,
                f"{task.task_id} 当前状态为 {task.status}，无需取消。",
            )
            return
        if not task.turn_id:
            self.store.update_task(task.task_id, status="cancelling")
            await self._reply(
                message.message_id,
                message.chat_id,
                "Worker 尚未报告 turn ID，已标记为等待取消。",
            )
            return
        await self.app.request(
            "turn/interrupt",
            {"threadId": task.worker_thread_id, "turnId": task.turn_id},
        )
        self.store.update_task(task.task_id, status="cancelling")
        await self._reply(
            message.message_id, message.chat_id, f"正在取消 {task.task_id}。"
        )

    async def _steer_task(self, message: IncomingMessage, rest: str) -> None:
        task_id, _, text = rest.partition(" ")
        task = self.store.get_task(task_id)
        if not task or task.chat_id != message.chat_id:
            await self._reply(message.message_id, message.chat_id, "没有找到该任务。")
            return
        if task.status != "running" or not task.turn_id or not text.strip():
            await self._reply(
                message.message_id,
                message.chat_id,
                "用法：/补充 task_xxxxxxxx 补充内容；任务必须正在运行。",
            )
            return
        await self.app.request(
            "turn/steer",
            {
                "threadId": task.worker_thread_id,
                "expectedTurnId": task.turn_id,
                "input": [{"type": "text", "text": text.strip()}],
                "clientUserMessageId": message.event_id,
            },
        )
        await self._reply(
            message.message_id, message.chat_id, "补充信息已发送给 worker。"
        )

    async def _resolve_approval(
        self, message: IncomingMessage, token: str, *, approve: bool
    ) -> None:
        pending = self._pending_approvals.get(token)
        if not pending or pending.chat_id != message.chat_id:
            await self._reply(message.message_id, message.chat_id, "没有找到该审批。")
            return
        result = _approval_result(pending.method, approve)
        await self.app.respond(pending.rpc_id, result)
        self._pending_approvals.pop(token, None)
        await self._reply(
            message.message_id,
            message.chat_id,
            f"已{'批准' if approve else '拒绝'} {token}。",
        )

    async def _reply(self, message_id: str, chat_id: str, text: str) -> None:
        if self.feishu is None:
            log.info("模拟回复 chat=%s message=%s：%s", chat_id, message_id, text)
            return
        await self.feishu.reply_text(message_id, chat_id, text)

    async def _send(self, chat_id: str, text: str) -> None:
        if self.feishu is None:
            log.info("模拟发送 chat=%s：%s", chat_id, text)
            return
        await self.feishu.send_text(chat_id, text)


def _looks_like_requirement_update(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized.startswith(REQUIREMENT_UPDATE_PREFIXES)


def _random_ack(*, is_update: bool) -> str:
    return choice(UPDATE_ACK_MESSAGES if is_update else ACK_MESSAGES)


def _format_tasks(tasks: list[TaskRecord]) -> str:
    if not tasks:
        return "当前会话还没有 worker 任务。"
    lines = ["最近任务："]
    for task in tasks:
        lines.append(f"- {task.task_id} [{task.status}] {task.title}")
    return "\n".join(lines)


def _format_task(task: TaskRecord | None) -> str:
    if task is None:
        return "没有找到该任务。"
    text = (
        f"{task.task_id}\n"
        f"状态：{task.status}\n"
        f"名称：{task.title}\n"
        f"创建时间：{task.created_at}"
    )
    if task.result:
        text += f"\n\n结果：\n{task.result}"
    return text


def _help_text() -> str:
    return (
        "飞书 Codex 命令：\n"
        "/任务：查看最近 worker\n"
        "/状态 task_xxx：查看任务详情\n"
        "/补充 task_xxx 内容：向运行中的 worker 追加信息\n"
        "/取消 task_xxx：中止 worker\n"
        "/项目：查看允许访问的项目\n"
        "/新对话：下条消息创建新的 Master 上下文\n"
        "/批准 approval_xxx：批准 Codex 操作\n"
        "/拒绝 approval_xxx：拒绝 Codex 操作"
    )


def _approval_summary(method: str, params: dict[str, Any]) -> str:
    if method == "item/commandExecution/requestApproval":
        command = params.get("command") or "未知命令"
        cwd = params.get("cwd") or "未知目录"
        reason = params.get("reason")
        summary = f"命令：{command}\n目录：{cwd}"
        return summary + (f"\n原因：{reason}" if reason else "")
    reason = params.get("reason") or "修改文件"
    root = params.get("grantRoot")
    return f"文件修改：{reason}" + (f"\n请求目录：{root}" if root else "")


def _approval_result(method: str, approve: bool) -> dict[str, str]:
    if method == "item/commandExecution/requestApproval":
        return {"decision": "accept" if approve else "decline"}
    if method == "item/fileChange/requestApproval":
        return {"decision": "approved" if approve else "denied"}
    raise ValueError(f"不支持的审批类型：{method}")


def _decline_result(method: str) -> dict[str, Any]:
    if method == "item/commandExecution/requestApproval":
        return {"decision": "decline"}
    if method == "item/fileChange/requestApproval":
        return {"decision": "denied"}
    if method == "item/permissions/requestApproval":
        return {"permissions": {"fileSystem": None, "network": None}}
    raise ValueError(f"不支持的反向请求：{method}")
