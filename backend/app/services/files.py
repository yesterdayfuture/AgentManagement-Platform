"""列出允许目录里的文件。

内置工具 files.list 调用 list_directory。路径相对 FILES_LIST_ROOT，
默认是 backend/data/file-root。绝对路径也必须落在这个根目录里面。
只返回这一层的名字、类型和大小，不读取文件内容，也不进入子目录。
解析后的真实路径必须仍在根目录内，避免通过 .. 或符号链接看到外面。
"""

import os
from pathlib import Path

from app.services.gateway import GateError

ENTRY_LIMIT = 200


def list_root() -> Path:
    """允许查看的根目录。环境变量 FILES_LIST_ROOT 可以改到别的位置。"""
    configured = os.environ.get("FILES_LIST_ROOT", "").strip()
    if configured:
        root = Path(configured).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[2] / "data" / "file-root"
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_directory(path: str) -> dict:
    """列出 path 这一层。path 为空时列出根目录本身。"""
    root = list_root()
    raw = (path or "").strip() or "."
    if "\x00" in raw:
        raise GateError("denied", "路径无效")
    candidate = Path(raw)
    requested = candidate if candidate.is_absolute() else root / candidate
    target = requested.resolve()
    if target != root and root not in target.parents:
        raise GateError("denied", "只能查看允许目录内的文件夹")
    if target.is_symlink():
        raise GateError("denied", "不跟随符号链接")
    if not target.is_dir():
        raise GateError("denied", "文件夹不存在")
    entries = []
    for child in sorted(target.iterdir(), key=lambda item: item.name):
        if len(entries) >= ENTRY_LIMIT:
            break
        if child.is_symlink():
            entries.append({"name": child.name, "kind": "link", "size": 0})
            continue
        kind = "dir" if child.is_dir() else "file"
        size = child.stat().st_size if kind == "file" else 0
        entries.append({"name": child.name, "kind": kind, "size": size})
    shown = str(target.relative_to(root)) if target != root else "."
    return {"path": shown, "root": str(root), "entries": entries}
