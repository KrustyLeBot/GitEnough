"""Claude through the local Claude Code CLI (`claude -p`), signed in with the user's own subscription.

No API key: the CLI carries its own sign-in. Every call is a separate headless run.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass

from .git_ops import GitError

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
COMMIT_MODEL = "haiku"
REVIEW_MODEL = "sonnet"
MODEL_CHOICES = [("haiku", "Haiku (fast)"), ("sonnet", "Sonnet"), ("opus", "Opus"), ("fable", "Fable"),
                 ("", "Claude Code default")]


class AIError(GitError):
    """Shown like git errors (ErrorDialog), with the CLI's own message."""


def find_cli() -> str | None:
    if os.environ.get("GITENOUGH_CLAUDE"):  # testing
        return os.environ["GITENOUGH_CLAUDE"]
    found = shutil.which("claude")
    if found:
        return found
    # The native installer puts it here without always updating the PATH of running apps.
    local = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
    return local if os.path.exists(local) else None


@dataclass
class CliStatus:
    path: str | None
    logged_in: bool = False
    detail: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.path) and self.logged_in


def status() -> CliStatus:
    path = find_cli()
    if not path:
        return CliStatus(None, detail="Claude Code is not installed")
    try:
        out = subprocess.run([path, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30, creationflags=NO_WINDOW).stdout
        data = json.loads(out[out.find("{"):] or "{}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return CliStatus(path, detail=f"Could not ask Claude Code for its sign-in state: {exc}")
    if not data.get("loggedIn"):
        return CliStatus(path, detail="Claude Code is not signed in")
    return CliStatus(path, True, str(data.get("authMethod") or ""))


def cli_version() -> str:
    path = find_cli()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=30, creationflags=NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip().split(" ")[0]


def open_update() -> None:
    """`claude update` in a console the user watches (it may restart or ask things)."""
    path = find_cli()
    if path:
        subprocess.Popen(["cmd", "/k", path, "update"], creationflags=subprocess.CREATE_NEW_CONSOLE)


def open_login() -> None:
    """Sign-in happens in the CLI itself (browser flow), in a console the user can see."""
    path = find_cli()
    if path:
        subprocess.Popen(["cmd", "/k", path, "auth", "login"], creationflags=subprocess.CREATE_NEW_CONSOLE)


def _explain(text: str) -> str:
    low = text.lower()
    if "oauth" in low or "authenticate" in low or "log in" in low or "login" in low:
        return ("Claude Code is not signed in (or its session expired). Click “Sign in to Claude” "
                "or run `claude auth login` in a terminal.\n\n" + text)
    return text


class Handle:
    """Lets another thread cancel a running call."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.models: list[str] = []  # models the CLI actually used (an alias maps to what it knows)

    def cancel(self):
        self.cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()


def run(prompt: str, model: str = "", *, cwd: str | None = None, tools: str = "", schema: dict | None = None,
        system: str = "", timeout: int = 600, on_event=None, handle: Handle | None = None, skills: bool = False):
    """One headless Claude run; returns the structured output (schema) or the text result.

    --safe-mode by default: the user's plugins, hooks and personal CLAUDE.md must not change the answer (a
    style hook would rewrite review comments); callers put the project context in the prompt themselves.
    skills=True keeps the personal skills loadable (a review skill), with every hook still disabled.
    tools: comma-separated built-in tools Claude may use, "" for none.
    on_event: called with each stream-json event (progress), from this worker thread.
    handle: lets the caller cancel the run.
    """
    path = find_cli()
    if not path:
        raise AIError("Claude Code is not installed. Install it from https://claude.com/claude-code, "
                      "then sign in with `claude auth login`.")
    args = [path, "-p", "--no-session-persistence", "--tools", tools, "--output-format",
            "stream-json" if on_event else "json"]
    if on_event:
        args.append("--verbose")  # stream-json requires it in print mode
    if model:
        args += ["--model", model]
    if skills:
        args += ["--settings", json.dumps({"disableAllHooks": True})]
    else:
        args.append("--safe-mode")
    if tools:
        args += ["--allowedTools", tools, "--permission-mode", "dontAsk"]
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    if system:
        args += ["--append-system-prompt", system]
    # stderr goes to a file: an unread pipe fills up and blocks the CLI while stdout streams.
    errors = tempfile.TemporaryFile()
    try:
        proc = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=errors, creationflags=NO_WINDOW)
    except OSError as exc:
        raise AIError(f"Could not start Claude Code: {exc}") from exc
    if handle is not None:
        handle.proc = proc
        if handle.cancelled:
            proc.kill()
    watchdog = threading.Timer(timeout, proc.kill)  # the streaming read below has no timeout of its own
    watchdog.daemon = True
    watchdog.start()
    final = None
    try:
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass  # The CLI exited early (cancelled or failed): its answer or stderr says why.
        if on_event:
            for raw in proc.stdout:
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if event.get("type") == "result":
                    final = event
                else:
                    on_event(event)
            proc.wait(timeout=timeout)
        else:
            out, _err = proc.communicate(timeout=timeout)
            text = out.decode("utf-8", errors="replace")
            final = json.loads(text[text.find("{"):]) if "{" in text else None
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise AIError(f"Claude did not answer within {timeout // 60} minutes.") from exc
    except ValueError as exc:
        raise AIError("Claude Code returned an unreadable answer.") from exc
    finally:
        watchdog.cancel()
    if handle is not None and handle.cancelled:
        raise AIError("Cancelled")
    if final is None:
        errors.seek(0)
        err = errors.read().decode("utf-8", errors="replace").strip()
        raise AIError(_explain(err or f"Claude Code stopped without an answer (exit code {proc.returncode})."))
    if handle is not None:
        handle.models = list((final.get("modelUsage") or {}).keys())
    if final.get("is_error") or final.get("subtype") != "success":
        raise AIError(_explain(str(final.get("result") or final.get("subtype") or "Claude Code failed")))
    if schema:
        data = final.get("structured_output")
        if data is None:
            try:
                data = json.loads(final.get("result") or "")
            except ValueError as exc:
                raise AIError("Claude did not return the expected structured answer.") from exc
        return data
    return str(final.get("result") or "").strip()


# ---------- commit message ----------

MAX_COMMIT_DIFF = 60_000  # characters of staged diff sent; the file list is always complete

COMMIT_PROMPT = """Write a git commit message for the staged changes below.

Rules:
- First line: imperative summary, at most 72 characters, no trailing period.
- Then, only if the change is not trivial: a blank line and a short body (wrapped at 72 characters)
  saying what changed and why. Bullets are fine for several independent changes.
- Match the language and conventions of the recent commit messages (prefixes like "feat:", ticket
  keys, capitalisation), if they follow one.
- Output the commit message only: no code fences, no preamble.

Branch: {branch}

Recent commit messages:
{recent}

Staged files:
{files}

Staged diff{cut}:
{diff}
"""


def commit_message(path: str, model: str = COMMIT_MODEL, handle: Handle | None = None) -> str:
    from .git_ops import run_git

    diff = run_git(["diff", "--cached", "--no-color", "--no-ext-diff", "-M", "--stat=200", "--patch"],
                   cwd=path, timeout=60)
    if not diff.strip():
        raise AIError("Nothing is staged: stage the changes to describe first.")
    files = run_git(["diff", "--cached", "--name-status", "-M"], cwd=path, timeout=30).strip()
    try:
        recent = run_git(["log", "-8", "--format=%s"], cwd=path, timeout=15).strip()
    except GitError:
        recent = ""
    try:
        branch = run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=path, timeout=15).strip()
    except GitError:
        branch = "(detached)"
    cut = ""
    if len(diff) > MAX_COMMIT_DIFF:
        diff, cut = diff[:MAX_COMMIT_DIFF], f" (cut to the first {MAX_COMMIT_DIFF} characters)"
    prompt = COMMIT_PROMPT.format(branch=branch, recent=recent or "(none)", files=files, diff=diff, cut=cut)
    text = run(prompt, model, cwd=path, timeout=180, handle=handle)
    return text.strip().strip("`").strip()
