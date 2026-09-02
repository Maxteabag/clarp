"""Atomic-enough simple TOML value updates shared by admin and server APIs."""
from __future__ import annotations

import json
from pathlib import Path


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def set_toml_value(path: Path, section: str, key: str, value) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    header = f"[{section}]"
    start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    rendered = f"{key} = {toml_value(value)}"
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, rendered])
    else:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].strip().startswith("[")), len(lines))
        target = next((i for i in range(start + 1, end)
                       if lines[i].split("=", 1)[0].strip() == key
                       and not lines[i].lstrip().startswith("#")), None)
        if target is None:
            lines.insert(end, rendered)
        else:
            lines[target] = rendered
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.next")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)
