<p align="center">
  <img src="docs/logo.png" width="96" alt="GitEnough logo">
</p>

<h1 align="center">GitEnough</h1>

<p align="center">
  <b>The last git tracker you'll need.</b><br>
  Every repository under one folder, in one window: status, pull, branches, diffs and history.
</p>

<p align="center">
  <a href="https://github.com/KrustyLeBot/GitEnough/raw/main/release/GitEnough.exe"><b>⬇ Download GitEnough.exe</b></a>
  &nbsp;·&nbsp; Windows 10 / 11 &nbsp;·&nbsp; single file, no install
</p>

<p align="center">
  <img src="docs/screenshots/main.png" alt="GitEnough main window">
</p>

## Why

You have dozens of repositories in `D:\Projects`. Some are behind, some have local changes, some sit on an old
feature branch, and one still points at the server you left last year. GitEnough finds them all and shows their
state at a glance, so you pull, switch and commit without opening each one.

## Features

**Overview**
- Finds every git repository under a root folder, at any depth, plus the URLs you list (clone them in one click).
- Status per repository: up to date, behind, ahead, diverged, local changes, stashes, off the base branch,
  rebase or merge in progress.
- **Needs rebase** flag: when the base branch moved on (`↓3 develop · Rebase`), one click opens the rebase
  assistant preset for that branch.
- Filters (behind, changes, off base, needs attention…), alphabetical order with pinned projects on top.
- **Pull all** clones what is missing and fast-forwards what is behind, in parallel.
- Refreshes a repository as soon as its files change, and fetches in the background on a timer.

**Changes and commits**
- Staged / unstaged files as a list or a collapsible folder tree, with a per-extension summary.
- Clean diff viewer: split or unified, syntax highlighting, word-level changes, find in diff.
- Stage, unstage or discard whole files, **single hunks or selected lines**.
- Commit, commit and push, ignore files (by name, extension or folder), discard all or stash instead.

**Branches and history**
- Commit graph of the current and base branches, commit details and per-file diffs.
- Create, rename, delete and clean up merged branches; tags; stashes (apply, pop, drop).
- Compare a branch with its base, like a merge request preview.
- File history and blame.
- **Rebase onto** assistant (`git rebase --onto`): pick the branch, the new base and the old base commit, see
  exactly which commits move, then force push **with lease** when done. Warns when the branch is behind its
  remote.
- **Built-in merge tool**: for every conflict, keep one side, both (in either order), neither, or write your own
  text. Sides are named after their branch (`origin/develop`, `feature/x`, `stash`), never "ours / theirs".
- **Reset to a remote branch**: after someone force-pushed, recreate your local branch from `origin/…` in one
  click, with your changes stashed and your old commits kept in a backup branch.
- Conflicts explained in plain words, with **Stash & retry** when local changes block a pull or a switch.

**Per repository**
- Change the remote URL (moving to another server takes a few seconds), custom name and base branch.
- Open in Visual Studio (`.sln` / `.slnx`), VS Code, Git Bash, the file explorer or the browser.
- Hide a repository, or delete its folder to the Recycle Bin after a check for unpushed work.

<p align="center">
  <img src="docs/screenshots/changes.png" alt="Changes window with hunk staging">
</p>

## Getting started

1. Install [Git for Windows](https://git-scm.com/download/win) if it is not there yet.
2. Download [`GitEnough.exe`](https://github.com/KrustyLeBot/GitEnough/raw/main/release/GitEnough.exe) and run it.
3. Open **Settings** (gear icon), pick your root folder, and add repository URLs if you want some cloned.
4. For HTTPS remotes, add a Personal Access Token per host in **Settings > Access (PAT)**. SSH remotes use your
   SSH keys.

## Updates

GitEnough checks this repository at start and every 6 hours. When a newer version is published, a notification
offers **Update now**: the new executable is downloaded, checked against the published size and SHA-256, swapped
in place of the running one, and restarted. *Settings > About > Check for updates* checks on demand.

## Security and privacy

- Tokens are stored in the **Windows Credential Manager** (encrypted with your Windows session). They are never
  written to the configuration file or into a remote URL, and never passed on a command line.
- The configuration lives in `%APPDATA%\GitEnough\config.json`. Export / import (Settings > General) shares the
  repository list with your team, without any token.
- Background git commands never take the index lock, so they cannot get in the way of your own git commands.
- No telemetry: GitEnough only talks to your git servers, plus this repository for the update check.

## Build from source

Requires Python on Windows (tested with Python 3.13).

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pyinstaller
.\build.ps1
```

The executable is written to `dist\GitEnough.exe` and copied to `release\`. To run without building:
`.\.venv\Scripts\python.exe main.py`.

## License

[MIT](LICENSE)

## Good enough

<p align="center">
  <img src="docs/screenshots/about.png" width="560" alt="Certified good enough by David Goodenough">
</p>

<p align="center"><i>“It's not perfect. It's good enough.”</i><br>David Goodenough</p>
