"""GitLab REST API (v4): merge requests to review, their diffs and discussions, and review comments.

Authentication is the host's personal access token from the keyring (scope "api").
"""

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import __version__, vault
from .config import CONFIG_DIR
from .git_ops import GitError

USER_AGENT = f"GitEnough/{__version__}"
MR_URL_RE = re.compile(r"^https?://([^/]+)/(.+?)/-/merge_requests/(\d+)")


class GitLabError(GitError):
    pass


def parse_mr_url(url: str) -> tuple[str, str, int] | None:
    """(host, project path, iid) of a merge request web link, else None."""
    m = MR_URL_RE.match(url.strip())
    return (m[1].lower(), m[2], int(m[3])) if m else None


NOT_GITLAB = ("github.com", "bitbucket.org", "dev.azure.com", "visualstudio.com", "codeberg.org")
_KIND: dict[str, bool] = {}  # host -> is a GitLab server (probed once per session)


def is_gitlab(host: str) -> bool:
    """Whether a host runs GitLab, whatever its name (self-hosted instances often have none of "gitlab").

    Asks its API: GitLab answers /api/v4/version with JSON, even with a 401 when no token is given.
    Blocking (network): call it from a worker thread.
    """
    host = host.lower()
    if host in _KIND:
        return _KIND[host]
    if not host or any(host == d or host.endswith("." + d) for d in NOT_GITLAB):
        result = False
    elif "gitlab" in host:
        result = True
    else:
        token = next((t for t in map(vault.get_token, vault.keys_of_host(host)) if t), None)
        base = os.environ.get("GITENOUGH_GITLAB_API") or f"https://{host}/api/v4"
        req = urllib.request.Request(base + "/version", headers={
            "User-Agent": USER_AGENT, **({"PRIVATE-TOKEN": token} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                result = "version" in json.loads(resp.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                result = exc.code in (401, 403) and "message" in json.loads(exc.read().decode("utf-8", "replace"))
            except ValueError:
                result = False
        except (urllib.error.URLError, OSError, ValueError):
            result = False
    _KIND[host] = result
    return result


def gitlab_hosts(hosts) -> list[str]:
    """The GitLab servers among these hosts (network probe, worker thread)."""
    hosts = sorted({h.lower() for h in hosts if h})
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(hosts)))) as pool:  # one probe per unknown host
        return [h for h, ok in zip(hosts, pool.map(is_gitlab, hosts)) if ok]


def mr_key(host: str, project: str, iid: int) -> str:
    return f"{host}/{project}!{iid}"


@dataclass
class MergeRequest:
    host: str
    project: str  # path with namespace
    iid: int
    title: str = ""
    author: str = ""
    author_username: str = ""
    web_url: str = ""
    source_branch: str = ""
    target_branch: str = ""
    updated: str = ""
    draft: bool = False
    state: str = "opened"
    notes: int = 0
    conflicts: bool = False
    description: str = ""
    reasons: set = field(default_factory=set)  # reviewer, assignee, mentioned, author, added
    base_sha: str = ""
    start_sha: str = ""
    head_sha: str = ""
    pipeline_status: str = ""  # success, failed, running, pending, canceled, manual, skipped, ... ("" = none)
    pipeline_url: str = ""
    pipeline_id: int = 0
    merge_status: str = ""  # detailed_merge_status: mergeable, not_approved, ci_must_pass, conflict, ...
    approvals_required: int = 0
    approved_by: list = field(default_factory=list)  # names
    user_approved: bool = False
    can_approve: bool = False

    @property
    def key(self) -> str:
        return mr_key(self.host, self.project, self.iid)


@dataclass
class FileDiff:
    new_path: str
    old_path: str
    diff: str
    new_file: bool = False
    deleted: bool = False
    renamed: bool = False
    too_large: bool = False  # GitLab sent no diff text (collapsed or over its limits)
    blob: str = ""  # the file's blob at the merge request's head ("" when unknown or deleted)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path

    @property
    def signature(self) -> str:
        """Changes when the file changes (new pushes): a viewed file becomes unviewed again.

        The blob at head when known: a rebase that leaves the file alone keeps it viewed, and a file too large
        for GitLab's diff still changes. Otherwise the diff text (diff_signature).
        """
        if self.blob:
            return "blob:" + self.blob
        if self.deleted:
            return "deleted"
        return self.diff_signature

    @property
    def diff_signature(self) -> str:
        """The signature of GitEnough 1.5.4 and before, still accepted for files viewed then."""
        return hashlib.sha1(self.diff.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class Note:
    id: int
    author: str
    username: str
    body: str
    created: str
    system: bool = False


@dataclass
class Discussion:
    id: str
    notes: list[Note]
    resolvable: bool = False
    resolved: bool = False
    new_path: str = ""
    old_path: str = ""
    new_line: int | None = None
    old_line: int | None = None

    @property
    def path(self) -> str:
        return self.new_path or self.old_path


class Client:
    def __init__(self, host: str, token: str):
        self.host = host
        # Overridable for testing, like the updater's URL.
        self.base = os.environ.get("GITENOUGH_GITLAB_API") or f"https://{host}/api/v4"
        self.token = token

    @classmethod
    def for_host(cls, host: str) -> "Client":
        return cls.for_key(host)

    @classmethod
    def for_project(cls, host: str, project: str) -> "Client":
        """Client with the token of the project's group when one is saved, else the host's."""
        key, token = vault.token_for(host, project)
        if not token:
            raise GitLabError(f"No access token for {host}/{project}. Add a personal access token with the “api” "
                              f"scope for {host} (or for its group) in Settings > Access (PAT).")
        return cls(host, token)

    @classmethod
    def for_key(cls, key: str) -> "Client":
        token = vault.get_token(key)
        if not token:
            raise GitLabError(f"No access token for {key}. Add a personal access token with the “api” scope "
                              "in Settings > Access (PAT).")
        return cls(vault.host_of(key), token)

    def _request(self, method: str, path: str, params: dict | None = None, body: dict | None = None,
                 timeout: int = 30, url: str = ""):
        url = (url or self.base + path) + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "PRIVATE-TOKEN": self.token, "User-Agent": USER_AGENT, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return (json.loads(raw.decode("utf-8")) if raw else None), resp.headers
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read().decode("utf-8", "replace"))
                detail = payload.get("message") or payload.get("error") or ""
            except (ValueError, AttributeError):
                pass
            if exc.code == 401:
                raise GitLabError(f"{self.host} refused the access token (expired or revoked). Update it in "
                                  "Settings > Access (PAT).") from exc
            if exc.code == 403:
                raise GitLabError(f"{self.host} denied the request: the token needs the “api” scope, or you "
                                  f"lack the rights on this project. {detail}".strip()) from exc
            if exc.code == 404:
                raise GitLabError(f"Not found on {self.host} (deleted, or not visible with this token). "
                                  f"{detail}".strip()) from exc
            raise GitLabError(f"{self.host} answered {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GitLabError(f"Could not reach {self.host}: {getattr(exc, 'reason', exc)}") from exc

    def get(self, path: str, **params):
        return self._request("GET", path, params)[0]

    def get_all(self, path: str, limit: int = 5000, **params) -> list:
        params = {"per_page": 100, **params}
        out, page = [], 1
        while page and len(out) < limit:
            data, headers = self._request("GET", path, {**params, "page": page}, timeout=60)
            out += data or []
            nxt = headers.get("X-Next-Page", "")
            page = int(nxt) if nxt.isdigit() else 0
        return out

    @staticmethod
    def pid(project: str) -> str:
        return urllib.parse.quote(project, safe="")

    # ---------- lists ----------
    def me(self) -> dict:
        return self.get("/user")

    def token_info(self) -> dict:
        """{"user", "scopes" (None when the server cannot tell), "expires"} of this token."""
        user = self.me()
        try:
            data = self.get("/personal_access_tokens/self")  # GitLab 15.5+
            scopes, expires = list(data.get("scopes") or []), data.get("expires_at") or ""
        except GitLabError:
            scopes, expires = None, ""
        return {"user": user.get("username", ""), "scopes": scopes, "expires": expires}

    def my_merge_requests(self) -> list[MergeRequest]:
        me = self.me()
        found: dict[str, MergeRequest] = {}

        def add(item: dict, reason: str):
            mr = self._mr(item)
            if mr.state != "opened":
                return
            found.setdefault(mr.key, mr).reasons.add(reason)

        common = {"state": "opened", "scope": "all"}
        queries = [("reviewer", "/merge_requests", {"reviewer_username": me["username"], **common}),
                   ("assignee", "/merge_requests", {"assignee_username": me["username"], **common}),
                   ("author", "/merge_requests", {"author_username": me["username"], **common}),
                   ("mentioned", "/todos", {"state": "pending", "type": "MergeRequest"})]
        # Independent lists: fetched at once, merged in a fixed order so the reasons do not depend on timing.
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            results = list(pool.map(lambda q: self.get_all(q[1], 500, **q[2]), queries))
        for (reason, _path, _params), items in zip(queries, results):
            for item in items:
                if reason != "mentioned":
                    add(item, reason)
                elif item.get("action_name") in ("mentioned", "directly_addressed") and item.get("target"):
                    add(item["target"], reason)
        return list(found.values())

    def _mr(self, item: dict) -> MergeRequest:
        refs = item.get("diff_refs") or {}
        project = (item.get("references") or {}).get("full", "").rsplit("!", 1)[0]
        if not project:
            project = parse_mr_url(item.get("web_url", ""))[1] if parse_mr_url(item.get("web_url", "")) else ""
        return MergeRequest(
            host=self.host, project=project, iid=int(item["iid"]), title=item.get("title", ""),
            author=(item.get("author") or {}).get("name", ""),
            author_username=(item.get("author") or {}).get("username", ""),
            web_url=item.get("web_url", ""), source_branch=item.get("source_branch", ""),
            target_branch=item.get("target_branch", ""), updated=item.get("updated_at", ""),
            draft=bool(item.get("draft") or item.get("work_in_progress")), state=item.get("state", ""),
            notes=int(item.get("user_notes_count") or 0), conflicts=bool(item.get("has_conflicts")),
            description=item.get("description") or "", base_sha=refs.get("base_sha", ""),
            start_sha=refs.get("start_sha", ""), head_sha=refs.get("head_sha", ""),
            pipeline_status=(item.get("head_pipeline") or {}).get("status", ""),
            pipeline_url=(item.get("head_pipeline") or {}).get("web_url", ""),
            pipeline_id=int((item.get("head_pipeline") or {}).get("id") or 0),
            merge_status=item.get("detailed_merge_status") or item.get("merge_status") or "")

    def merge_request(self, project: str, iid: int) -> MergeRequest:
        mr = self._mr(self.get(f"/projects/{self.pid(project)}/merge_requests/{iid}"))
        try:
            self.fill_approvals(mr)
        except GitLabError:
            pass  # approvals unavailable (rights, GitLab edition): the rest of the review works
        return mr

    # ---------- pipeline ----------
    def pipeline(self, project: str, pipeline_id: int) -> dict:
        return self.get(f"/projects/{self.pid(project)}/pipelines/{pipeline_id}") or {}

    def pipeline_jobs(self, project: str, pipeline_id: int) -> list[dict]:
        """Jobs and bridges (child / downstream pipelines) of a pipeline, latest attempt of each."""
        base = f"/projects/{self.pid(project)}/pipelines/{pipeline_id}"
        jobs = self.get_all(f"{base}/jobs", 500)
        try:
            for b in self.get_all(f"{base}/bridges", 200):
                b["bridge"] = True
                jobs.append(b)
        except GitLabError:
            pass
        return jobs

    def job_log(self, project: str, job_id: int, limit: int = 400_000) -> str:
        """The end of a job's log (the useful part when it failed)."""
        url = f"{self.base}/projects/{self.pid(project)}/jobs/{job_id}/trace"
        req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self.token, "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise GitLabError(f"{self.host} answered {exc.code} for the job log") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GitLabError(f"Could not reach {self.host}: {getattr(exc, 'reason', exc)}") from exc
        return raw[-limit:].decode("utf-8", "replace")

    def fill_approvals(self, mr: MergeRequest) -> None:
        data = self.get(f"/projects/{self.pid(mr.project)}/merge_requests/{mr.iid}/approvals") or {}
        mr.approved_by = [(a.get("user") or {}).get("name", "") for a in data.get("approved_by") or []]
        mr.approvals_required = int(data.get("approvals_required") or 0)
        mr.user_approved = bool(data.get("user_has_approved"))
        mr.can_approve = bool(data.get("user_can_approve"))

    def approve(self, mr: MergeRequest, approve: bool) -> None:
        path = f"/projects/{self.pid(mr.project)}/merge_requests/{mr.iid}/{'approve' if approve else 'unapprove'}"
        # sha: GitLab refuses the approval if new commits arrived since the diff was read.
        self._request("POST", path, body={"sha": mr.head_sha} if approve and mr.head_sha else {})

    # ---------- content ----------
    def diffs(self, project: str, iid: int, progress=None) -> list[FileDiff]:
        path = f"/projects/{self.pid(project)}/merge_requests/{iid}/diffs"
        out, page = [], 1
        while page:
            data, headers = self._request("GET", path, {"per_page": 100, "page": page}, timeout=120)
            for d in data or []:
                text = d.get("diff") or ""
                out.append(FileDiff(d.get("new_path", ""), d.get("old_path", ""), text,
                                    bool(d.get("new_file")), bool(d.get("deleted_file")),
                                    bool(d.get("renamed_file")),
                                    not text and bool(d.get("too_large") or d.get("collapsed"))))
            if progress:
                progress(len(out), int(headers.get("X-Total", 0) or 0))
            nxt = headers.get("X-Next-Page", "")
            page = int(nxt) if nxt.isdigit() else 0
        return out

    def blob_ids(self, project: str, ref: str, paths: list[str]) -> dict[str, str]:
        """{path: blob id} of files at a commit, 100 paths per GraphQL request; missing paths are left out."""
        query = ("query($project: ID!, $ref: String!, $paths: [String!]!) { project(fullPath: $project) "
                 "{ repository { blobs(ref: $ref, paths: $paths) { nodes { path oid } } } } }")
        graphql = self.base.rsplit("/v4", 1)[0] + "/graphql"
        out = {}
        for i in range(0, len(paths), 100):
            data, _h = self._request("POST", "", url=graphql, timeout=60,
                                     body={"query": query, "variables": {"project": project, "ref": ref,
                                                                         "paths": paths[i:i + 100]}})
            repo = (((data or {}).get("data") or {}).get("project") or {}).get("repository") or {}
            out.update({n["path"]: n["oid"] for n in (repo.get("blobs") or {}).get("nodes") or []
                        if n.get("path") and n.get("oid")})
        return out

    def discussions(self, project: str, iid: int) -> list[Discussion]:
        items = self.get_all(f"/projects/{self.pid(project)}/merge_requests/{iid}/discussions")
        out = []
        for item in items:
            notes = [Note(n["id"], (n.get("author") or {}).get("name", ""), (n.get("author") or {}).get("username", ""),
                          n.get("body", ""), n.get("created_at", ""), bool(n.get("system")))
                     for n in item.get("notes", [])]
            if not notes or all(n.system for n in notes):
                continue  # "added 3 commits", "approved this merge request", ...
            first = item["notes"][0]
            pos = first.get("position") or {}
            out.append(Discussion(item["id"], [n for n in notes if not n.system], bool(first.get("resolvable")),
                                  bool(first.get("resolved")), pos.get("new_path") or "", pos.get("old_path") or "",
                                  pos.get("new_line"), pos.get("old_line")))
        return out

    def raw_file(self, project: str, path: str, ref: str) -> str | None:
        """A file's text at a commit, or None when it does not exist."""
        quoted = urllib.parse.quote(path, safe="")
        try:
            url = f"{self.base}/projects/{self.pid(project)}/repository/files/{quoted}/raw?ref={ref}"
            req = urllib.request.Request(url, headers={"PRIVATE-TOKEN": self.token, "User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise GitLabError(f"{self.host} answered {exc.code} for {path}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GitLabError(f"Could not reach {self.host}: {getattr(exc, 'reason', exc)}") from exc

    def draft_count(self, project: str, iid: int) -> int:
        return len(self.get_all(f"/projects/{self.pid(project)}/merge_requests/{iid}/draft_notes"))

    # ---------- writes ----------
    def reply(self, project: str, iid: int, discussion: str, body: str) -> None:
        self._request("POST", f"/projects/{self.pid(project)}/merge_requests/{iid}/discussions/{discussion}/notes",
                      body={"body": body})

    def resolve(self, project: str, iid: int, discussion: str, resolved: bool) -> None:
        self._request("PUT", f"/projects/{self.pid(project)}/merge_requests/{iid}/discussions/{discussion}",
                      {"resolved": "true" if resolved else "false"})

    def add_draft(self, mr: MergeRequest, body: str, file: FileDiff | None = None, new_line: int | None = None,
                  old_line: int | None = None) -> None:
        payload: dict = {"note": body}
        if file is not None and (new_line or old_line):
            position = {"position_type": "text", "base_sha": mr.base_sha, "start_sha": mr.start_sha,
                        "head_sha": mr.head_sha, "new_path": file.new_path or file.old_path,
                        "old_path": file.old_path or file.new_path}
            if new_line:
                position["new_line"] = new_line
            if old_line:
                position["old_line"] = old_line
            payload["position"] = position
        self._request("POST", f"/projects/{self.pid(mr.project)}/merge_requests/{mr.iid}/draft_notes",
                      body=payload)

    def publish_drafts(self, mr: MergeRequest) -> None:
        self._request("POST", f"/projects/{self.pid(mr.project)}/merge_requests/{mr.iid}/draft_notes/bulk_publish")


# ---------- local review state (viewed files, comments not sent yet) ----------

STATE_PATH = CONFIG_DIR / "reviews.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), "utf-8")
    os.replace(tmp, STATE_PATH)
