from __future__ import annotations

import unittest

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from feishu_codex.feishu import _lark_log_level, _parse_incoming, _split_text


class FeishuMessageTest(unittest.TestCase):
    def test_lark_sdk_never_uses_info_or_debug_logging(self) -> None:
        self.assertEqual(_lark_log_level("DEBUG"), lark.LogLevel.WARNING)
        self.assertEqual(_lark_log_level("INFO"), lark.LogLevel.WARNING)
        self.assertEqual(_lark_log_level("ERROR"), lark.LogLevel.ERROR)

    def test_parses_text_and_removes_bot_mention(self) -> None:
        event = P2ImMessageReceiveV1(
            {
                "header": {"event_id": "evt-1"},
                "event": {
                    "sender": {
                        "sender_type": "user",
                        "sender_id": {"open_id": "ou-user"},
                    },
                    "message": {
                        "message_id": "om-1",
                        "chat_id": "oc-1",
                        "chat_type": "group",
                        "message_type": "text",
                        "content": '{"text":"@_user_1 运行测试"}',
                        "mentions": [{"key": "@_user_1", "name": "Codex"}],
                    },
                },
            }
        )
        incoming = _parse_incoming(event)
        self.assertEqual(incoming.event_id, "evt-1")
        self.assertEqual(incoming.sender_open_id, "ou-user")
        self.assertEqual(incoming.text, "运行测试")

    def test_splits_long_text_on_newline(self) -> None:
        chunks = _split_text("a" * 700 + "\n" + "b" * 700, 1000)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("a"))
        self.assertTrue(chunks[1].startswith("b"))


if __name__ == "__main__":
    unittest.main()
