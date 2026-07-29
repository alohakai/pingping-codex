from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_type: str
    text: str
    message_type: str


IncomingHandler = Callable[[IncomingMessage], Awaitable[None]]


class FeishuBot:
    def __init__(self, settings: Settings, on_message: IncomingHandler) -> None:
        self.settings = settings
        self.on_message = on_message
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._api = (
            lark.Client.builder()
            .app_id(settings.feishu_app_id)
            .app_secret(settings.feishu_app_secret)
            .log_level(_lark_log_level(settings.log_level))
            .build()
        )
        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_sdk_message)
            .build()
        )
        self._ws = lark.ws.Client(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            event_handler=dispatcher,
            log_level=_lark_log_level(settings.log_level),
        )

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self._ws_thread = threading.Thread(
            target=self._run_ws, name="feishu-ws", daemon=True
        )
        self._ws_thread.start()
        log.info("飞书长连接线程已启动")

    async def reply_text(self, message_id: str, chat_id: str, text: str) -> None:
        chunks = _split_text(text, self.settings.reply_max_chars)
        if not chunks:
            return
        await asyncio.to_thread(self._reply_text_sync, message_id, chunks[0])
        for chunk in chunks[1:]:
            await asyncio.to_thread(self._send_text_sync, chat_id, chunk)

    async def send_text(self, chat_id: str, text: str) -> None:
        for chunk in _split_text(text, self.settings.reply_max_chars):
            await asyncio.to_thread(self._send_text_sync, chat_id, chunk)

    def _run_ws(self) -> None:
        try:
            self._ws.start()
        except Exception:
            log.exception("飞书长连接异常退出")

    def _on_sdk_message(self, data: P2ImMessageReceiveV1) -> None:
        try:
            incoming = _parse_incoming(data)
        except Exception:
            log.exception("解析飞书消息失败")
            return
        if incoming.sender_type != "user":
            return
        if self.settings.allowed_users and (
            incoming.sender_open_id not in self.settings.allowed_users
        ):
            log.warning("拒绝未授权飞书用户：%s", incoming.sender_open_id)
            return
        if (
            self.settings.allowed_chats
            and incoming.chat_id not in self.settings.allowed_chats
        ):
            log.warning("拒绝未授权飞书会话：%s", incoming.chat_id)
            return
        if self.loop is None:
            log.error("飞书消息到达时主事件循环尚未就绪")
            return
        future = asyncio.run_coroutine_threadsafe(self.on_message(incoming), self.loop)
        future.add_done_callback(_log_future_error)

    def _reply_text_sync(self, message_id: str, text: str) -> None:
        body = (
            ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(_text_content(text))
            .uuid(uuid.uuid4().hex)
            .build()
        )
        request = (
            ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = self._api.im.v1.message.reply(request)
        _raise_for_response(response, "回复飞书消息")

    def _send_text_sync(self, chat_id: str, text: str) -> None:
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(_text_content(text))
            .uuid(uuid.uuid4().hex)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        response = self._api.im.v1.message.create(request)
        _raise_for_response(response, "发送飞书消息")


def _parse_incoming(data: P2ImMessageReceiveV1) -> IncomingMessage:
    if data.event is None or data.event.message is None or data.event.sender is None:
        raise ValueError("飞书事件缺少 message 或 sender")
    message = data.event.message
    sender = data.event.sender
    sender_id = sender.sender_id
    message_type = message.message_type or ""
    text = ""
    if message_type == "text":
        payload = json.loads(message.content or "{}")
        text = str(payload.get("text", ""))
        for mention in message.mentions or []:
            if mention.key:
                text = text.replace(mention.key, "")
        text = text.strip()
    event_id = (
        data.header.event_id
        if data.header is not None and data.header.event_id
        else message.message_id
    )
    return IncomingMessage(
        event_id=event_id or uuid.uuid4().hex,
        message_id=message.message_id or "",
        chat_id=message.chat_id or "",
        chat_type=message.chat_type or "",
        sender_open_id=sender_id.open_id if sender_id and sender_id.open_id else "",
        sender_type=sender.sender_type or "",
        text=text,
        message_type=message_type,
    )


def _text_content(text: str) -> str:
    return json.dumps({"text": text}, ensure_ascii=False)


def _split_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = max_chars
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _raise_for_response(response: object, action: str) -> None:
    if response.success():
        return
    raise RuntimeError(
        f"{action}失败：code={getattr(response, 'code', None)}, "
        f"msg={getattr(response, 'msg', None)}, "
        f"log_id={response.get_log_id()}"
    )


def _lark_log_level(level: str) -> lark.LogLevel:
    # lark-oapi 的长连接 INFO/DEBUG 日志会包含临时 access_key 和 ticket。
    # 即使网关本身开启 DEBUG，也不允许 SDK 输出低于 WARNING 的日志。
    if level == "ERROR":
        return lark.LogLevel.ERROR
    return lark.LogLevel.WARNING


def _log_future_error(future: object) -> None:
    try:
        future.result()
    except Exception:
        log.exception("处理飞书消息失败")
