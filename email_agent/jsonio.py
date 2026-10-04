"""Small helper for writing JSON files (pydantic models or plain data)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def dump_json(path: Path, data: Any, *, by_alias: bool = False, exclude_none: bool = False) -> None:
    """Write JSON with a trailing newline. Models are dumped in JSON mode;
    lists of models are dumped item by item. `by_alias=True` writes wire-format
    names (e.g. MCP's camelCase `inputSchema`); with `exclude_none=True` empty
    optional fields are omitted, as on the wire."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, BaseModel):
        text = data.model_dump_json(indent=2, by_alias=by_alias, exclude_none=exclude_none)
    else:
        if isinstance(data, list) and data and isinstance(data[0], BaseModel):
            data = [d.model_dump(mode="json", by_alias=by_alias, exclude_none=exclude_none) for d in data]
        text = json.dumps(data, indent=2, default=str, ensure_ascii=False)
    path.write_text(text + "\n")
