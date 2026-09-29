import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "GitEnough"
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
DRAFTS_PATH = CONFIG_DIR / "drafts.json"
# The app was first released as GitTracker: its configuration is picked up once.
LEGACY_CONFIG_PATH = Path(os.environ.get("APPDATA") or Path.home()) / "GitTracker" / "config.json"


@dataclass
class Config:
    root: str = ""
    repos: list[str] = field(default_factory=list)
    # Projects unchecked by the user (folder ids; URLs in older configs), excluded from "pull all".
    disabled: list[str] = field(default_factory=list)
    pinned: list[str] = field(default_factory=list)  # folder ids kept at the top of the list
    hidden: list[str] = field(default_factory=list)  # folder ids not listed
    # folder id -> {"title": display name, "base": base branch} chosen in the repository settings
    repo_overrides: dict[str, dict] = field(default_factory=dict)
    # host -> username used with the PAT; the PAT itself lives in the OS keyring.
    hosts: dict[str, str] = field(default_factory=dict)
    # First existing branch wins; origin/HEAD is used when none exists.
    base_branches: list[str] = field(default_factory=lambda: ["develop", "main", "master"])
    auto_fetch_minutes: int = 10  # 0 disables periodic fetch
    file_tree: bool = False  # file lists shown as a folder tree
    list_filter: str = "all"  # main list state filter
    watch_files: bool = True  # refresh a repository as soon as its files change
    geometry: str = ""
    # window kind ("changes", "history", ...) -> [width, height, maximized], restored when one opens
    window_sizes: dict[str, list] = field(default_factory=dict)
    # Claude Code CLI models ("haiku", "sonnet", a full model name, or "" for the CLI's default)
    ai_commit_model: str = "haiku"
    ai_review_model: str = "sonnet"
    mr_links: list[str] = field(default_factory=list)  # merge requests added by URL
    review_skill_source: str = ""  # GitLab project whose CI artifacts hold .skill files
    review_skill: str = ""  # name of the skill installed from it for the AI review ("" = built-in method)

    # ---------- commit message drafts, per repository folder (typed or suggested by Claude) ----------
    @staticmethod
    def load_draft(repo: str) -> str:
        try:
            return json.loads(DRAFTS_PATH.read_text("utf-8")).get(os.path.normcase(repo), "")
        except (OSError, ValueError):
            return ""

    @staticmethod
    def save_draft(repo: str, text: str) -> None:
        try:
            drafts = json.loads(DRAFTS_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            drafts = {}
        key = os.path.normcase(repo)
        if text.strip():
            drafts[key] = text
        else:
            drafts.pop(key, None)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = DRAFTS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(drafts, indent=1), "utf-8")
        os.replace(tmp, DRAFTS_PATH)

    @classmethod
    def load(cls) -> "Config":
        source = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
        try:
            data = json.loads(source.read_text("utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), "utf-8")
        # Atomic replace so a crash never leaves a half-written config.
        os.replace(tmp, CONFIG_PATH)
