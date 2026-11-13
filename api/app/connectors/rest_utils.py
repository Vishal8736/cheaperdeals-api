"""Small helpers for the no-code REST connectors: dotted-path lookup and {placeholder} templating."""
from __future__ import annotations

import json
import re
from typing import Any

_PH = re.compile(r"\{([a-zA-Z0-9_\.]+)\}")


def dig(obj: Any, path: str, default: Any = None) -> Any:
    """dig({"a":[{"b":1}]}, "a.0.b") -> 1. Empty path returns obj."""
    if path in ("", None):
        return obj
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return cur


def render(template: Any, ctx: dict) -> Any:
    """Replace {name} in every string of a JSON-like structure. A string that is exactly "{name}" keeps the value's type
    (so {lines} can become a list). Unknown names are left as they are."""
    if isinstance(template, str):
        m = _PH.fullmatch(template)
        if m and m.group(1) in ctx:
            return ctx[m.group(1)]
        return _PH.sub(lambda mm: str(ctx[mm.group(1)]) if mm.group(1) in ctx else mm.group(0), template)
    if isinstance(template, list):
        return [render(x, ctx) for x in template]
    if isinstance(template, dict):
        if set(template) == {"__json__"}:  # {"__json__": {...}} becomes a JSON *string* (some APIs want an object inside a text field)
            return json.dumps(render(template["__json__"], ctx), separators=(",", ":"), ensure_ascii=False)
        return {k: render(v, ctx) for k, v in template.items()}
    return template
