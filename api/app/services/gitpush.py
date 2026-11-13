"""Put this project's code on GitHub with just a token (GitHub REST API, no git program needed), so Render can build it."""
from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from . import shopcfg as C

API = "https://api.github.com"
ROOT = Path(__file__).resolve().parents[3]  # the project folder (has render.yaml and api/)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}
SKIP_NAMES = {"local.env", ".env"}
SKIP_SUFFIX = (".db", ".db-wal", ".db-shm", ".zip", ".pyc", ".log")
MAX_FILE = 2_000_000
SLEEP = time.sleep  # tests replace this


class GitError(Exception):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


SKIPPED: list[str] = []  # files that GitHub refused for lack of the token's `workflow` scope (filled by publish, read by Start for a warning)
WORKFLOW_DIR = ".github/workflows/"


def project_files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT)
        if not p.is_file() or set(rel.parts) & SKIP_DIRS or p.name in SKIP_NAMES or p.name.endswith(SKIP_SUFFIX) or p.stat().st_size > MAX_FILE:
            continue
        out.append(p)
    return out


def _call(token: str, method: str, path: str, **kw):
    r = C.client().request(method, API + path, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}, **kw)
    return r


def _ok(r, what: str):
    if r.status_code >= 300:
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = r.text[:150]
        hint = {401: "GitHub token galat/expire.", 403: "Token ko repo likhne ki permission nahi (classic token: 'repo' scope; fine-grained: Contents + Administration write).",
                404: "Token ko is repo/file ki permission nahi ('repo' aur '.github/workflows' ke liye 'workflow' scope chahiye)."}.get(r.status_code, "")
        raise GitError(f"GitHub ({what}) ne mana kiya ({r.status_code}). {hint} {msg}".strip(), r.status_code)
    return r.json() if r.content else {}


def repo_exists(token: str, name: str) -> dict:
    """Look only: who is logged in and whether github.com/<you>/<name> already exists."""
    me = _ok(_call(token, "GET", "/user"), "user")["login"]
    r = _call(token, "GET", f"/repos/{me}/{name}")
    return {"me": me, "exists": r.status_code == 200, "url": f"https://github.com/{me}/{name}"}


def _tree(token: str, base: str, entries: list[dict]) -> dict:
    return _ok(_call(token, "POST", base + "/git/trees", json={"tree": entries}), "files upload")


def publish(token: str, name: str, private: bool = False, message: str = "Cheape Deal") -> str:
    """Create (or reuse) github.com/<you>/<name> and make its main branch equal to this project's files. Returns the repo URL. Secrets files are never uploaded."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", name or ""):
        raise GitError("Repo ka naam sirf akshar, number, '-', '_' ya '.' ho sakta hai.")
    me = _ok(_call(token, "GET", "/user"), "user")["login"]
    r = _call(token, "POST", "/user/repos", json={"name": name, "private": private, "auto_init": True, "description": "Cheape Deal store"})
    if r.status_code == 422:  # already exists: reuse it
        _ok(_call(token, "GET", f"/repos/{me}/{name}"), "repo")
    else:
        _ok(r, "repo banana")
    base = f"/repos/{me}/{name}"
    head = None
    for _ in range(6):  # a brand-new repo needs a moment before its first commit shows up
        g = _call(token, "GET", base + "/git/ref/heads/main")
        if g.status_code == 200:
            head = g.json()["object"]["sha"]
            break
        SLEEP(2)
    if head is None:
        raise GitError("Naye repo ka main branch nahi mila. Thodi der baad dobara try karo.")
    entries = []
    for p in project_files():
        rel = p.relative_to(ROOT).as_posix()
        raw = p.read_bytes()
        try:
            if not raw:
                raise UnicodeDecodeError("utf-8", b"", 0, 1, "empty")  # empty files go through the blob route
            entries.append({"path": rel, "mode": "100644", "type": "blob", "content": raw.decode("utf-8")})
        except UnicodeDecodeError:
            b = _ok(_call(token, "POST", base + "/git/blobs", json={"content": base64.b64encode(raw).decode(), "encoding": "base64"}), "file")
            entries.append({"path": rel, "mode": "100644", "type": "blob", "sha": b["sha"]})
    SKIPPED.clear()
    try:
        tree = _tree(token, base, entries)
    except GitError as e:
        # `gh auth login` tokens often lack the `workflow` scope; GitHub then refuses (404/403/422) the WHOLE tree as soon as it holds
        # .github/workflows/*. The keep-alive workflow is optional (the server pings itself), so upload everything else.
        wf = [en for en in entries if en["path"].startswith(WORKFLOW_DIR)]
        if not wf or e.status not in (403, 404, 422):
            raise
        rest = [en for en in entries if not en["path"].startswith(WORKFLOW_DIR)]
        tree = _tree(token, base, rest)
        SKIPPED.extend(en["path"] for en in wf)
    commit = _ok(_call(token, "POST", base + "/git/commits", json={"message": message, "tree": tree["sha"], "parents": [head]}), "commit")
    _ok(_call(token, "PATCH", base + "/git/refs/heads/main", json={"sha": commit["sha"], "force": True}), "branch update")
    return f"https://github.com/{me}/{name}"
