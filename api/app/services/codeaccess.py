"""READ-ONLY view of this project's own source code for the built-in AI.

The AI may look at the code to answer "how does X work", "where is Y", to suggest improvements and to explain an error. It has NO way to change anything:
this module only ever opens files for reading, there is no write/delete/rename function here, and nothing in the AI path can reach one (a test checks both).
Secrets never go in: env files, databases, key files and anything under the data/log folders are excluded, and every excerpt is run through the secret remover."""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import diag

ROOT = Path(__file__).resolve().parents[3]  # the project folder (the one that holds api/, tests/, docs/)
TEXT_EXT = {".py", ".html", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".js", ".css", ".bat", ".sh", ".ini", ".cfg", ".example", ".conf"}
SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "logs", "data", "dist", "build", ".vercel", ".render"}
SKIP_NAMES = {"tailwind.css", "package-lock.json", "local.env", "cheapedeal.env", ".env"}
SKIP_PATTERNS = re.compile(r"(\.env($|\.)|secret|\.pem$|\.key$|\.db$|\.sqlite|\.zip$|\.png$|\.jpg$|\.ico$|\.woff)", re.I)
MAX_FILE = 200_000
_cache: dict = {"at": 0.0, "files": []}


def allowed(rel: str) -> bool:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        return False
    if any(part in SKIP_DIRS for part in p.parts) or p.name in SKIP_NAMES or SKIP_PATTERNS.search(p.name):
        return False
    return p.suffix.lower() in TEXT_EXT or p.name.endswith(".env.example")


def files(refresh: bool = False) -> list[str]:
    """Relative paths the AI may read (cached for a minute)."""
    if not refresh and time.monotonic() - _cache["at"] < 60 and _cache["files"]:
        return _cache["files"]
    out = []
    for f in ROOT.rglob("*"):
        try:
            if not f.is_file():
                continue
            rel = f.relative_to(ROOT).as_posix()
            if allowed(rel) and f.stat().st_size <= MAX_FILE:
                out.append(rel)
        except (OSError, ValueError):
            continue
    _cache.update(at=time.monotonic(), files=sorted(out))
    return _cache["files"]


def read(rel: str, start: int = 1, end: int = 0, max_chars: int = 6000) -> str:
    """Lines start..end (1-based) of an allowed file, numbered, secrets removed. '' when the file is not allowed or missing."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not allowed(rel):
        return ""
    f = (ROOT / rel).resolve()
    try:
        f.relative_to(ROOT.resolve())  # no escaping the project folder (symlinks, ..)
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    end = end or min(len(lines), start + 80)
    body = "\n".join(f"{i}: {ln}" for i, ln in enumerate(lines[start - 1:end], start))
    return diag.redact(body)[:max_chars]


def _words(q: str) -> list[str]:
    stop = {"the", "and", "for", "kya", "hai", "kaise", "kaun", "mein", "kahan", "where", "what", "how", "this", "that", "with", "from", "code", "file", "function", "batao", "bata", "karta", "karti", "hota", "hoti"}
    return [w for w in re.findall(r"[a-z0-9_]{3,}", (q or "").lower()) if w not in stop][:8]


def search(query: str, limit: int = 4, window: int = 5) -> list[dict]:
    """Best matching places in the code for a question: [{file, line, text}] (text = a few numbered lines, secrets removed)."""
    ws = _words(query)
    if not ws:
        return []
    scored = []
    for rel in files():
        try:
            text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        flat = low.replace("_", "")
        hits = sum(low.count(w) for w in ws)
        distinct = sum(1 for w in ws if w in low or w.replace("_", "") in flat)  # how many of the asked words appear at all
        rel_flat = rel.lower().replace("_", "").replace("/", "")
        path_hits = sum(1 for w in ws if w.replace("_", "") in rel_flat)
        if hits or distinct or path_hits:
            scored.append((path_hits * 8 + distinct * 4 + min(hits, 20) + (3 if rel.startswith("api/app/") else 0) - (3 if rel.startswith("tests/") else 0), rel, text))
    out = []
    for _, rel, text in sorted(scored, reverse=True)[:limit]:
        lines = text.splitlines()
        best, best_n = 0, -1
        for i, ln in enumerate(lines):  # the line that mentions the most words, preferring definitions
            n = sum(w in ln.lower().replace("_", "") or w in ln.lower() for w in ws) + (2 if re.match(r"\s*(def|class|async def)\b", ln) else 0)
            if n > best_n:
                best, best_n = i, n
        a, b = max(0, best - 1), min(len(lines), best + window)
        out.append({"file": rel, "line": a + 1, "text": diag.redact("\n".join(f"{i + 1}: {ln}" for i, ln in enumerate(lines[a:b], a)))[:900]})
    return out


def outline(max_chars: int = 5000) -> str:
    """One line per module of the app (what it is for): a map the AI keeps in mind."""
    rows = []
    for rel in files():
        if rel.startswith("api/app/") and rel.endswith(".py") and "/__" not in rel:
            try:
                head = (ROOT / rel).read_text(encoding="utf-8", errors="replace").lstrip()[:400]
            except OSError:
                continue
            m = re.match(r'(?:"""|\'\'\')(.+?)(?:\n|"""|\'\'\')', head)
            rows.append(f"{rel[8:]}: {(m.group(1).strip() if m else '')[:70]}")
    return "\n".join(rows)[:max_chars]


def context_for(message: str) -> str:
    """Text added to the AI's prompt when the person is allowed to see code: module map + the best matching excerpts."""
    hits = search(message)
    parts = ["MODULE MAP (api/app/...):\n" + outline()]
    for h in hits:
        parts.append(f"--- {h['file']} (from line {h['line']}) ---\n{h['text']}")
    return "\n".join(parts)
