from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from feishu_codex.store import StateStore


class StateStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_event_deduplication(self) -> None:
        self.assertTrue(self.store.mark_event_once("evt-1"))
        self.assertFalse(self.store.mark_event_once("evt-1"))

    def test_chat_and_task_lifecycle(self) -> None:
        binding = self.store.bind_chat("chat-1", "master-1")
        self.assertEqual(binding.master_thread_id, "master-1")
        self.assertEqual(
            self.store.get_chat_by_master("master-1").chat_id,  # type: ignore[union-attr]
            "chat-1",
        )

        task = self.store.create_task(
            chat_id="chat-1",
            master_thread_id="master-1",
            worker_thread_id="worker-1",
            prompt="检查登录模块并运行测试",
        )
        self.assertEqual(task.status, "running")
        self.assertEqual(
            self.store.create_task(
                chat_id="chat-1",
                master_thread_id="master-1",
                worker_thread_id="worker-1",
                prompt="不会重复创建",
            ).task_id,
            task.task_id,
        )

        updated = self.store.update_task(
            task.task_id,
            status="completed",
            turn_id="turn-1",
            result="测试通过",
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.result, "测试通过")  # type: ignore[union-attr]
        self.assertEqual(self.store.list_tasks("chat-1")[0].status, "completed")

    def test_reset_chat_keeps_tasks(self) -> None:
        self.store.bind_chat("chat-1", "master-1")
        self.store.create_task(
            chat_id="chat-1",
            master_thread_id="master-1",
            worker_thread_id="worker-1",
            prompt="后台任务",
        )
        self.assertEqual(self.store.reset_chat("chat-1"), "master-1")
        self.assertIsNone(self.store.get_chat("chat-1"))
        self.assertEqual(len(self.store.list_tasks("chat-1")), 1)


if __name__ == "__main__":
    unittest.main()
