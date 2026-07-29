from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from .app_server import AppServerClient
from .config import Settings
from .feishu import FeishuBot, IncomingMessage
from .orchestrator import Orchestrator
from .store import StateStore

log = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="飞书 Codex Master/Worker 网关")
    parser.add_argument(
        "--check",
        action="store_true",
        help="检查配置、数据库和 Codex App Server，不连接飞书",
    )
    parser.add_argument(
        "--simulate",
        metavar="TEXT",
        help="不连接飞书，向模拟会话发送一条消息（会真实调用 Codex）",
    )
    parser.add_argument(
        "--simulate-wait-workers",
        metavar="SECONDS",
        type=int,
        default=0,
        help="模拟消息后继续等待 worker 和 Master 汇报，最多等待指定秒数",
    )
    args = parser.parse_args()

    root = Path.cwd().resolve()
    _load_dotenv(root / ".env")
    settings = Settings.from_env(root)
    _configure_logging(settings.log_level)

    errors = settings.validate(require_feishu=not (args.check or args.simulate))
    if errors:
        raise SystemExit("配置检查失败：\n- " + "\n- ".join(errors))

    if args.check:
        asyncio.run(_check(settings))
    elif args.simulate:
        asyncio.run(
            _simulate(settings, args.simulate, max(0, args.simulate_wait_workers))
        )
    else:
        asyncio.run(_serve(settings))


async def _check(settings: Settings) -> None:
    store = StateStore(settings.state_db)
    app = AppServerClient(settings.codex_binary)
    try:
        await app.start()
        models = await app.request(
            "model/list", {"limit": 1, "includeHidden": False}, timeout=30
        )
        model = (models.get("data") or [{}])[0].get("id", "未发现模型")
        print("配置检查通过")
        print(f"Codex CLI: {settings.codex_binary}")
        print(f"Codex 模型: {model}")
        print(f"Master 目录: {settings.master_cwd}")
        print(f"项目: {', '.join(settings.projects)}")
        print(f"状态库: {store.export_summary()}")
    finally:
        await app.stop()
        store.close()


async def _simulate(settings: Settings, text: str, wait_workers: int) -> None:
    store = StateStore(settings.state_db)
    app = AppServerClient(settings.codex_binary)
    orchestrator = Orchestrator(settings, store, app)
    try:
        await app.start()
        await orchestrator.handle_message(
            IncomingMessage(
                event_id=f"simulate-{os.urandom(8).hex()}",
                message_id="simulate-message",
                chat_id="simulate-chat",
                chat_type="p2p",
                sender_open_id="simulate-user",
                sender_type="user",
                text=text,
                message_type="text",
            )
        )
        queue = orchestrator._queues.get("simulate-chat")
        if queue:
            await queue.join()
        if wait_workers:
            deadline = asyncio.get_running_loop().time() + wait_workers
            while asyncio.get_running_loop().time() < deadline:
                tasks = store.list_tasks("simulate-chat")
                if tasks and all(
                    task.status in {"completed", "failed", "interrupted", "cancelled"}
                    for task in tasks
                ):
                    break
                await asyncio.sleep(0.5)
            queue = orchestrator._queues.get("simulate-chat")
            if queue:
                await queue.join()
            print(f"模拟 worker 状态：{store.export_summary()['tasks']}")
    finally:
        await app.stop()
        store.close()


async def _serve(settings: Settings) -> None:
    store = StateStore(settings.state_db)
    app = AppServerClient(settings.codex_binary)
    orchestrator = Orchestrator(settings, store, app)
    feishu = FeishuBot(settings, orchestrator.handle_message)
    orchestrator.attach_feishu(feishu)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await app.start()
        feishu.start(loop)
        log.info("飞书 Codex 网关已运行，按 Ctrl+C 停止")
        await stop_event.wait()
    finally:
        await app.stop()
        store.close()


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


if __name__ == "__main__":
    main()
