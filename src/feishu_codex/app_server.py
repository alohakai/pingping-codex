from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

JsonObject = dict[str, Any]
NotificationHandler = Callable[[JsonObject], Awaitable[None] | None]
ServerRequestHandler = Callable[[JsonObject], Awaitable[object] | object]
DEFERRED = object()


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self, codex_binary: Path) -> None:
        self.codex_binary = codex_binary
        self.process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._pending: dict[int | str, asyncio.Future[JsonObject]] = {}
        self._send_lock = asyncio.Lock()
        self._read_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._notification_handlers: list[NotificationHandler] = []
        self._server_request_handler: ServerRequestHandler | None = None
        self._closed = False

    def add_notification_handler(self, handler: NotificationHandler) -> None:
        self._notification_handlers.append(handler)

    def set_server_request_handler(self, handler: ServerRequestHandler) -> None:
        self._server_request_handler = handler

    async def start(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            str(self.codex_binary),
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._read_task = asyncio.create_task(self._read_loop(), name="codex-read")
        self._stderr_task = asyncio.create_task(
            self._stderr_loop(), name="codex-stderr"
        )
        await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "feishu_codex",
                    "title": "Feishu Codex Gateway",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})
        log.info("Codex App Server 已初始化，pid=%s", self.process.pid)

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._read_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        self._fail_pending(AppServerError("Codex App Server 已停止"))
        self.process = None

    async def request(
        self, method: str, params: JsonObject | None = None, timeout: float = 30
    ) -> JsonObject:
        if self.process is None or self.process.returncode is not None:
            raise AppServerError("Codex App Server 未运行")
        self._request_id += 1
        request_id = self._request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[JsonObject] = loop.create_future()
        self._pending[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params or {}})
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise AppServerError(f"Codex 请求超时：{method}") from exc

    async def notify(self, method: str, params: JsonObject | None = None) -> None:
        await self._send({"method": method, "params": params or {}})

    async def respond(self, request_id: int | str, result: object) -> None:
        await self._send({"id": request_id, "result": result})

    async def respond_error(
        self, request_id: int | str, code: int, message: str
    ) -> None:
        await self._send(
            {"id": request_id, "error": {"code": code, "message": message}}
        )

    async def _send(self, message: JsonObject) -> None:
        process = self.process
        if process is None or process.stdin is None or process.returncode is not None:
            raise AppServerError("Codex App Server 输入流不可用")
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._send_lock:
            process.stdin.write(raw.encode("utf-8"))
            await process.stdin.drain()

    async def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("忽略无法解析的 App Server 输出：%r", line[:500])
                    continue
                if "id" in message and ("result" in message or "error" in message):
                    self._handle_response(message)
                elif "id" in message and "method" in message:
                    asyncio.create_task(
                        self._handle_server_request(message),
                        name=f"codex-server-request-{message['id']}",
                    )
                elif "method" in message:
                    for handler in tuple(self._notification_handlers):
                        asyncio.create_task(
                            _invoke(handler, message),
                            name=f"codex-notify-{message['method']}",
                        )
                else:
                    log.debug("未知 App Server 消息：%s", message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("读取 Codex App Server 输出失败")
        finally:
            if not self._closed:
                self._fail_pending(AppServerError("Codex App Server 输出流已关闭"))

    async def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.info("codex: %s", text)

    def _handle_response(self, message: JsonObject) -> None:
        request_id = message["id"]
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        if "error" in message:
            error = message["error"]
            future.set_exception(
                AppServerError(
                    f"Codex RPC 错误 {error.get('code')}: {error.get('message')}"
                )
            )
        else:
            future.set_result(message.get("result") or {})

    async def _handle_server_request(self, message: JsonObject) -> None:
        request_id = message["id"]
        handler = self._server_request_handler
        if handler is None:
            await self.respond_error(request_id, -32601, "Client request unsupported")
            return
        try:
            result = await _invoke_result(handler, message)
            if result is not DEFERRED:
                await self.respond(request_id, result)
        except Exception as exc:
            log.exception("处理 App Server 反向请求失败：%s", message.get("method"))
            await self.respond_error(request_id, -32000, str(exc))

    def _fail_pending(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()


async def _invoke(handler: NotificationHandler, message: JsonObject) -> None:
    result = handler(message)
    if inspect.isawaitable(result):
        await result


async def _invoke_result(handler: ServerRequestHandler, message: JsonObject) -> object:
    result = handler(message)
    if inspect.isawaitable(result):
        return await result
    return result
