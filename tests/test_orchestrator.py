from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feishu_codex.config import Settings
from feishu_codex.feishu import IncomingMessage
from feishu_codex.orchestrator import (
    ACK_MESSAGES,
    UPDATE_ACK_MESSAGES,
    Orchestrator,
    _looks_like_requirement_update,
    _random_ack,
)
from feishu_codex.store import StateStore


class FakeApp:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    def add_notification_handler(self, handler: object) -> None:
        self.notification_handler = handler

    def set_server_request_handler(self, handler: object) -> None:
        self.server_request_handler = handler

    async def request(
        self, method: str, params: dict[str, object]
    ) -> dict[str, object]:
        self.requests.append((method, params))
        return {}


class OrchestratorEventTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")
        settings = Settings.from_env(Path.cwd())
        self.app = FakeApp()
        self.orchestrator = Orchestrator(settings, self.store, self.app)  # type: ignore[arg-type]
        self.store.bind_chat("chat-1", "master-1")

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    async def test_registers_subagent_activity(self) -> None:
        await self.orchestrator.on_notification(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "master-1",
                    "turnId": "turn-1",
                    "item": {
                        "type": "subAgentActivity",
                        "kind": "started",
                        "agentThreadId": "worker-1",
                        "agentPath": "/root/test-worker",
                    },
                },
            }
        )
        task = self.store.get_task_by_worker("worker-1")
        self.assertIsNotNone(task)
        self.assertEqual(task.chat_id, "chat-1")  # type: ignore[union-attr]
        self.assertEqual(task.status, "running")  # type: ignore[union-attr]

    async def test_live_update_steers_active_master(self) -> None:
        self.orchestrator._active_master_turns["chat-1"] = (
            "master-1",
            "turn-master-1",
        )
        master_steered, worker = await self.orchestrator._route_live_update(
            IncomingMessage(
                event_id="event-update-1",
                message_id="message-update-1",
                chat_id="chat-1",
                chat_type="p2p",
                sender_open_id="user-1",
                sender_type="user",
                text="改成蓝色主题",
                message_type="text",
            )
        )
        self.assertTrue(master_steered)
        self.assertIsNone(worker)
        method, params = self.app.requests[-1]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["expectedTurnId"], "turn-master-1")

    async def test_live_update_steers_single_running_worker(self) -> None:
        task = self.store.create_task(
            chat_id="chat-1",
            master_thread_id="master-1",
            worker_thread_id="worker-2",
            prompt="构建页面",
        )
        self.store.update_task(task.task_id, turn_id="turn-worker-2")
        master_steered, worker = await self.orchestrator._route_live_update(
            IncomingMessage(
                event_id="event-update-2",
                message_id="message-update-2",
                chat_id="chat-1",
                chat_type="p2p",
                sender_open_id="user-1",
                sender_type="user",
                text="补充：需要深色模式",
                message_type="text",
            )
        )
        self.assertFalse(master_steered)
        self.assertEqual(worker.task_id, task.task_id)  # type: ignore[union-attr]
        method, params = self.app.requests[-1]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["expectedTurnId"], "turn-worker-2")


class OrchestratorHelperTest(unittest.TestCase):
    def test_random_ack_uses_expected_pool(self) -> None:
        self.assertIn(_random_ack(is_update=False), ACK_MESSAGES)
        self.assertIn(_random_ack(is_update=True), UPDATE_ACK_MESSAGES)

    def test_detects_requirement_update_prefixes(self) -> None:
        self.assertTrue(_looks_like_requirement_update("改成蓝色主题"))
        self.assertTrue(_looks_like_requirement_update("补充：再加一个筛选器"))
        self.assertFalse(_looks_like_requirement_update("今天天气怎么样"))


if __name__ == "__main__":
    unittest.main()
