from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CODEX_BINARY = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
VALID_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}
VALID_APPROVAL_POLICIES = {"untrusted", "on-request", "never"}


def _csv_set(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True)
class Project:
    alias: str
    path: Path
    description: str
    sandbox: str


@dataclass(frozen=True)
class Settings:
    root: Path
    feishu_app_id: str
    feishu_app_secret: str
    allowed_users: frozenset[str]
    allowed_chats: frozenset[str]
    codex_binary: Path
    codex_model: str | None
    codex_sandbox: str
    codex_approval_policy: str
    master_cwd: Path
    projects: dict[str, Project]
    memory_dir: Path
    state_db: Path
    log_level: str
    reply_max_chars: int

    @property
    def workspace_roots(self) -> list[str]:
        roots = {str(self.master_cwd.resolve())}
        roots.update(str(project.path.resolve()) for project in self.projects.values())
        roots.add(str(self.memory_dir.resolve()))
        return sorted(roots)

    @classmethod
    def from_env(cls, root: Path | None = None) -> Settings:
        root = (root or Path.cwd()).resolve()
        codex_value = os.getenv("CODEX_BINARY", "").strip()
        if codex_value:
            codex_binary = Path(codex_value).expanduser()
        elif DEFAULT_CODEX_BINARY.exists():
            codex_binary = DEFAULT_CODEX_BINARY
        else:
            discovered = shutil.which("codex")
            codex_binary = Path(discovered) if discovered else Path("codex")

        sandbox = os.getenv("CODEX_SANDBOX", "workspace-write").strip()
        if sandbox not in VALID_SANDBOXES:
            raise ValueError(f"CODEX_SANDBOX 必须是 {sorted(VALID_SANDBOXES)} 之一")

        approval_policy = os.getenv("CODEX_APPROVAL_POLICY", "on-request").strip()
        if approval_policy not in VALID_APPROVAL_POLICIES:
            raise ValueError(
                f"CODEX_APPROVAL_POLICY 必须是 {sorted(VALID_APPROVAL_POLICIES)} 之一"
            )

        projects_file = _resolve(
            root, os.getenv("CODEX_PROJECTS_FILE", "config/projects.json")
        )
        projects = _load_projects(projects_file)
        master_cwd = _resolve(root, os.getenv("CODEX_MASTER_CWD", str(root)))
        memory_dir = _resolve(root, os.getenv("CODEX_MEMORY_DIR", "memory"))
        state_db = _resolve(root, os.getenv("STATE_DB", "data/state.db"))

        return cls(
            root=root,
            feishu_app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            allowed_users=_csv_set(os.getenv("FEISHU_ALLOWED_USERS")),
            allowed_chats=_csv_set(os.getenv("FEISHU_ALLOWED_CHATS")),
            codex_binary=codex_binary,
            codex_model=os.getenv("CODEX_MODEL", "").strip() or None,
            codex_sandbox=sandbox,
            codex_approval_policy=approval_policy,
            master_cwd=master_cwd,
            projects=projects,
            memory_dir=memory_dir,
            state_db=state_db,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            reply_max_chars=max(
                1000, int(os.getenv("FEISHU_REPLY_MAX_CHARS", "12000"))
            ),
        )

    def validate(self, require_feishu: bool = True) -> list[str]:
        errors: list[str] = []
        if require_feishu and not self.feishu_app_id:
            errors.append("缺少 FEISHU_APP_ID")
        if require_feishu and not self.feishu_app_secret:
            errors.append("缺少 FEISHU_APP_SECRET")
        if (
            not self.codex_binary.exists()
            and shutil.which(str(self.codex_binary)) is None
        ):
            errors.append(f"找不到 Codex CLI：{self.codex_binary}")
        if not self.master_cwd.is_dir():
            errors.append(f"Master 工作目录不存在：{self.master_cwd}")
        for alias, project in self.projects.items():
            if not project.path.is_dir():
                errors.append(f"项目 {alias} 的路径不存在：{project.path}")
        return errors


def _load_projects(path: Path) -> dict[str, Project]:
    if not path.exists():
        raise ValueError(f"项目配置不存在：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("projects.json 必须是非空对象")

    projects: dict[str, Project] = {}
    for alias, value in raw.items():
        if (
            not isinstance(alias, str)
            or not alias.strip()
            or not isinstance(value, dict)
        ):
            raise ValueError("projects.json 的项目格式无效")
        project_path = Path(str(value.get("path", ""))).expanduser()
        if not project_path.is_absolute():
            project_path = (path.parent / project_path).resolve()
        sandbox = str(value.get("sandbox", "workspace-write"))
        if sandbox not in VALID_SANDBOXES:
            raise ValueError(f"项目 {alias} 的 sandbox 无效：{sandbox}")
        projects[alias] = Project(
            alias=alias,
            path=project_path.resolve(),
            description=str(value.get("description", "")),
            sandbox=sandbox,
        )
    return projects
