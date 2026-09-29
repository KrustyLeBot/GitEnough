"""Code review skill built by a GitLab CI pipeline: found in the job artifacts, installed for Claude Code.

The source is any GitLab project whose pipelines publish `.skill` files (zip archives holding a SKILL.md)
as artifacts. The chosen skill goes to ~/.claude/skills/<name>/, where the Claude Code CLI loads personal
skills. Claude Desktop keeps its skills in the claude.ai account instead: this module never touches those.
"""

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass

from .gitlab import USER_AGENT, Client, GitLabError

SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "skills")
MARKER = ".gitenough-skill.json"  # marks a skill folder GitEnough installed and may replace
MAX_ARTIFACTS = 300 * 1024 * 1024
PIPELINES_TO_SCAN = 10  # newest successful pipelines looked at when the latest has no skill


def parse_source(url: str) -> tuple[str, str] | None:
    """(host, project path) from any link inside a GitLab project (repo, pipeline, file, clone URL)."""
    url = url.strip()
    m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?/?$", url)
    if m:
        return m[1].lower(), m[2]
    parsed = urllib.parse.urlparse(url)
    if not parsed.hostname or not parsed.path.strip("/"):
        return None
    path = parsed.path.split("/-/", 1)[0].strip("/")
    path = path[:-4] if path.endswith(".git") else path
    return (parsed.hostname.lower(), path) if "/" in path else None


@dataclass
class SkillFile:
    name: str  # from SKILL.md (folder name under ~/.claude/skills)
    file: str  # .skill path inside the artifacts
    description: str
    pipeline: int
    job: int
    job_name: str
    sha256: str
    created: str
    web_url: str = ""

    @property
    def label(self) -> str:
        return f"{self.name}  ·  pipeline #{self.pipeline} ({self.created[:10]})"


def _skill_meta(data: bytes) -> tuple[str, str]:
    """(name, description) from the SKILL.md front matter of a .skill archive."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        entry = next((n for n in z.namelist() if n.rsplit("/", 1)[-1] == "SKILL.md" and n.count("/") <= 1), None)
        if entry is None:
            raise GitLabError("Not a skill archive: no SKILL.md inside")
        text = z.read(entry).decode("utf-8", "replace")
    front = text.split("---", 2)[1] if text.startswith("---") else ""
    meta = dict(re.findall(r"^(\w[\w-]*):\s*(.*)$", front, re.M))
    folder = entry.rsplit("/", 1)[0] if "/" in entry else ""
    name = (meta.get("name") or folder or "review-skill").strip().strip("'\"")
    return re.sub(r"[^\w.-]", "-", name), meta.get("description", "").strip().strip("'\"")


class Source:
    def __init__(self, url: str):
        parsed = parse_source(url)
        if not parsed:
            raise GitLabError("Not a GitLab project link. Paste the project's URL, e.g. https://gitlab.com/group/"
                              "project")
        self.host, self.project = parsed
        self.client = Client.for_project(self.host, self.project)
        self._zips: dict[int, bytes] = {}
        self.errors: list[str] = []  # artifact downloads that failed (rights, expiry)

    def _pid(self) -> str:
        return Client.pid(self.project)

    def _download(self, job: int) -> bytes:
        if job not in self._zips:
            url = f"{self.client.base}/projects/{self._pid()}/jobs/{job}/artifacts"
            req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self.client.token, "User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = resp.read(MAX_ARTIFACTS + 1)
            except urllib.error.HTTPError as exc:
                why = {401: "the token was refused", 403: "your role on the project does not allow downloading "
                       "job artifacts (Reporter or higher is needed)", 404: "they expired or were deleted"}
                raise GitLabError(f"Artifacts of job {job} not available: {why.get(exc.code, exc.reason)}") from exc
            except OSError as exc:
                raise GitLabError(f"Could not download the artifacts of job {job}: {exc}") from exc
            if len(data) > MAX_ARTIFACTS:
                raise GitLabError(f"The artifacts of job {job} are over {MAX_ARTIFACTS // 2**20} MB")
            self._zips[job] = data
        return self._zips[job]

    def pipelines(self) -> list[dict]:
        branch = self.client.get(f"/projects/{self._pid()}").get("default_branch") or "main"
        return self.client.get(f"/projects/{self._pid()}/pipelines", ref=branch, status="success",
                               order_by="id", sort="desc", per_page=PIPELINES_TO_SCAN) or []

    def skills_in(self, pipeline: dict) -> list[SkillFile]:
        jobs = self.client.get_all(f"/projects/{self._pid()}/pipelines/{pipeline['id']}/jobs", 200,
                                   **{"scope[]": "success"})
        found = []
        for job in jobs:
            if not any(a.get("file_type") == "archive" for a in job.get("artifacts") or []) and not job.get(
                    "artifacts_file"):
                continue
            try:
                archive = zipfile.ZipFile(io.BytesIO(self._download(job["id"])))
            except GitLabError as exc:
                self.errors.append(str(exc))
                continue
            except zipfile.BadZipFile:
                continue  # not a zip archive: nothing to offer from this job
            for entry in archive.namelist():
                if not entry.lower().endswith(".skill"):
                    continue
                data = archive.read(entry)
                try:
                    name, description = _skill_meta(data)
                except (zipfile.BadZipFile, GitLabError):
                    continue
                found.append(SkillFile(name, entry, description, int(pipeline["id"]), int(job["id"]),
                                       job.get("name", ""), hashlib.sha256(data).hexdigest(),
                                       pipeline.get("created_at", ""), pipeline.get("web_url", "")))
        return found

    def latest_skills(self) -> list[SkillFile]:
        """Skills of the newest successful pipeline that published any (older ones may have expired)."""
        for pipeline in self.pipelines():
            skills = self.skills_in(pipeline)
            if skills:
                return skills
        if self.errors:
            raise GitLabError(self.errors[0])
        return []

    def skill_bytes(self, skill: SkillFile) -> bytes:
        with zipfile.ZipFile(io.BytesIO(self._download(skill.job))) as z:
            return z.read(skill.file)


# ---------- installation for the Claude Code CLI ----------

def installed_dir(name: str) -> str:
    return os.path.join(SKILLS_DIR, name)


def installed_info(name: str) -> dict | None:
    try:
        with open(os.path.join(installed_dir(name), MARKER), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def install(skill: SkillFile, data: bytes) -> str:
    """Unpack the .skill into ~/.claude/skills/<name>/, replacing only a copy GitEnough installed."""
    target = installed_dir(skill.name)
    if os.path.exists(target) and installed_info(skill.name) is None:
        raise GitLabError(f"A skill named “{skill.name}” already exists in {SKILLS_DIR} and was not installed by "
                          "GitEnough: it is left alone. Rename or remove it to use this one.")
    os.makedirs(SKILLS_DIR, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=f".{skill.name}-", dir=SKILLS_DIR)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for member in z.infolist():
                parts = [p for p in member.filename.replace("\\", "/").split("/") if p not in ("", ".")]
                if ".." in parts or not parts:
                    continue  # never write outside the skill folder
                z.extract(member, staging)
        entries = os.listdir(staging)
        root = staging
        if "SKILL.md" not in entries and len(entries) == 1 and os.path.isdir(os.path.join(staging, entries[0])):
            root = os.path.join(staging, entries[0])  # archive holding a single <name>/ folder
        with open(os.path.join(root, MARKER), "w", encoding="utf-8") as fh:
            json.dump({**asdict(skill), "installed": time.time()}, fh, indent=1)
        if os.path.exists(target):
            shutil.rmtree(target)
        shutil.move(root, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return target


def uninstall(name: str) -> None:
    if name and installed_info(name) is not None:
        shutil.rmtree(installed_dir(name), ignore_errors=True)


@dataclass
class CheckResult:
    status: str  # "current", "updated", "installed", "missing", "error"
    message: str = ""
    skill: SkillFile | None = None


def check(source_url: str, name: str) -> CheckResult:
    """Keep the chosen skill in line with the newest pipeline: install, update, or report it missing."""
    try:
        src = Source(source_url)
        pipelines = src.pipelines()
        info = installed_info(name)
        if info and pipelines and info.get("pipeline") == pipelines[0]["id"]:
            return CheckResult("current", skill=SkillFile(**{k: info[k] for k in SkillFile.__dataclass_fields__
                                                             if k in info}))
        for pipeline in pipelines:
            skills = src.skills_in(pipeline)
            if not skills:
                continue
            match = next((s for s in skills if s.name == name), None)
            if match is None:
                return CheckResult("missing", f"The review skill “{name}” is no longer built by {src.project}: the "
                                              f"newest pipeline (#{pipeline['id']}) publishes "
                                              + ", ".join(sorted({s.name for s in skills})) + ".")
            if info and info.get("sha256") == match.sha256 and os.path.exists(installed_dir(name)):
                return CheckResult("current", skill=match)
            install(match, src.skill_bytes(match))
            return CheckResult("updated" if info else "installed",
                               f"Review skill “{name}” {'updated' if info else 'installed'} from pipeline "
                               f"#{match.pipeline}", match)
        if info and os.path.exists(installed_dir(name)):
            return CheckResult("current", "No pipeline with skills left (artifacts expired): keeping the "
                                          "installed copy.")
        return CheckResult("missing", f"No successful pipeline of {src.project} publishes the review skill "
                                      f"“{name}” any more.")
    except (GitLabError, OSError, zipfile.BadZipFile) as exc:
        return CheckResult("error", str(exc))


# ---------- access checks for the settings page ----------

@dataclass
class Check:
    ok: bool | None  # None: a warning, not a failure
    title: str
    detail: str = ""


ROLES = {10: "Guest", 15: "Planner", 20: "Reporter", 30: "Developer", 40: "Maintainer", 50: "Owner"}


def check_token(host: str) -> list[Check]:
    """host: a token key, a host ("gitlab.com") or a group of it ("gitlab.com/some-org")."""
    from . import vault

    if not vault.get_token(host):
        return [Check(False, f"{host}: no access token", "Add a personal access token with the “api” scope in "
                                                         "Settings > Access (PAT).")]
    try:
        info = Client.for_key(host).token_info()
    except GitLabError as exc:
        return [Check(False, f"{host}: token not usable", str(exc))]
    out = [Check(True, f"{host}: token valid, signed in as {info['user']}")]
    scopes = info["scopes"]
    if scopes is None:
        out.append(Check(None, f"{host}: token scopes unknown", "This GitLab version does not report them. "
                                                                "Reviews need the “api” scope."))
    elif "api" not in scopes:
        out.append(Check(False, f"{host}: the token lacks the “api” scope",
                         f"It has {', '.join(scopes) or 'no scope'}. Merge request lists, comments and "
                         "artifacts need “api”: create a new token with it."))
    else:
        out.append(Check(True, f"{host}: scopes {', '.join(scopes)}"))
    if info["expires"]:
        days = (time.mktime(time.strptime(info["expires"][:10], "%Y-%m-%d")) - time.time()) / 86400
        if days < 14:
            out.append(Check(None if days >= 0 else False, f"{host}: token expires on {info['expires'][:10]}",
                             "Renew it in GitLab, then update it in Settings > Access (PAT)."))
    return out


def check_source(url: str) -> list[Check]:
    parsed = parse_source(url)
    if not parsed:
        return [Check(False, "Skill source: not a GitLab project link")]
    host, project = parsed
    try:
        src = Source(url)
        data = src.client.get(f"/projects/{src._pid()}")
    except GitLabError as exc:
        return [Check(False, f"Skill source {project}: not reachable", str(exc))]
    perms = data.get("permissions") or {}
    level = max((perms.get(k) or {}).get("access_level") or 0 for k in ("project_access", "group_access"))
    role = ROLES.get(level, "no direct role (public or internal project)")
    out = [Check(True, f"Skill source {project}: visible, your role: {role}")]
    try:
        pipelines = src.pipelines()
    except GitLabError as exc:
        return out + [Check(False, "Cannot read its pipelines", f"{exc} Reporter or higher is usually needed.")]
    if not pipelines:
        return out + [Check(None, "No successful pipeline on its default branch yet")]
    out.append(Check(True, f"Pipelines readable (newest successful: #{pipelines[0]['id']})"))
    try:
        src.client.get(f"/projects/{src._pid()}/pipelines/{pipelines[0]['id']}/jobs", per_page=1)
        out.append(Check(True, "Jobs readable (artifacts are checked by “Look for skills”)"))
    except GitLabError as exc:
        out.append(Check(False, "Cannot read its jobs, so neither their artifacts", str(exc)))
    return out
