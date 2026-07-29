from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_codex.config import Settings


class SettingsTest(unittest.TestCase):
    def test_loads_project_registry_and_workspace_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "project"
            project.mkdir()
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "projects.json").write_text(
                json.dumps(
                    {
                        "demo": {
                            "path": str(project),
                            "description": "测试项目",
                            "sandbox": "read-only",
                        }
                    }
                ),
                encoding="utf-8",
            )
            env = {
                "CODEX_MASTER_CWD": str(root),
                "CODEX_PROJECTS_FILE": "config/projects.json",
                "STATE_DB": "data/test.db",
            }
            with patch.dict(os.environ, env, clear=True):
                settings = Settings.from_env(root)
            self.assertEqual(settings.projects["demo"].path, project.resolve())
            self.assertIn(str(project.resolve()), settings.workspace_roots)
            self.assertEqual(settings.memory_dir, (root / "memory").resolve())
            self.assertIn(str((root / "memory").resolve()), settings.workspace_roots)
            self.assertEqual(settings.state_db, (root / "data/test.db").resolve())

    def test_rejects_invalid_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_dir = root / "config"
            config_dir.mkdir()
            (config_dir / "projects.json").write_text(
                json.dumps(
                    {
                        "demo": {
                            "path": str(root),
                            "sandbox": "workspace-write",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_MASTER_CWD": str(root),
                        "CODEX_PROJECTS_FILE": "config/projects.json",
                        "CODEX_SANDBOX": "invalid",
                    },
                    clear=True,
                ),
                self.assertRaises(ValueError),
            ):
                Settings.from_env(root)


if __name__ == "__main__":
    unittest.main()
