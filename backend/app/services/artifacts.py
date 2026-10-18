"""从工作流输出里取出项目文件，并打成压缩包供执行记录下载。"""

import io
import re
import zipfile
from pathlib import PurePosixPath

from app.util import load

_MARKED = re.compile(r"<<<FILE\s+([^\n>]+)>>>\s*\n(.*?)\n?<<<END>>>", re.S)
_FENCE = re.compile(
    r"```[ \t]*[A-Za-z0-9_+-]*[ \t]+(?P<path>(?:[\w.-]+/)+[\w.-]+\.[\w.-]+)\s*\n(?P<body>.*?)```",
    re.S,
)
_MAX_FILES = 40
_MAX_FILE = 200_000
_MAX_TOTAL = 2_000_000


def _safe_path(name: str) -> str:
    text = (name or "").strip().replace("\\", "/").lstrip("/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        return ""
    if any(not part or part.startswith(".") for part in path.parts):
        return ""
    if len(path.parts) > 8:
        return ""
    return path.as_posix()


def parse_files(text: str) -> dict[str, str]:
    """识别两种写法：<<<FILE 路径>>> 正文 <<<END>>>，以及带路径的 Markdown 代码块。"""
    found = {}
    for match in _MARKED.finditer(text or ""):
        name = _safe_path(match.group(1))
        body = match.group(2)
        if name and len(body.encode("utf-8")) <= _MAX_FILE:
            found[name] = body
    for match in _FENCE.finditer(text or ""):
        name = _safe_path(match.group("path"))
        body = match.group("body")
        if name and name not in found and len(body.encode("utf-8")) <= _MAX_FILE:
            found[name] = body
    if len(found) > _MAX_FILES:
        kept = list(found.items())[:_MAX_FILES]
        found = dict(kept)
    return found


def project_files(run) -> dict[str, str]:
    """合并最终输出和每一步输出。同名文件以后出现的为准。"""
    texts = []
    for item in load(getattr(run, "log_json", "[]"), []):
        if isinstance(item, dict):
            texts.append(item.get("output") or "")
    texts.append(getattr(run, "output_text", "") or "")
    found = {}
    total = 0
    for text in texts:
        for name, body in parse_files(text).items():
            size = len(body.encode("utf-8"))
            if total + size > _MAX_TOTAL:
                continue
            total += size
            found[name] = body
    return found


def project_zip(run) -> bytes | None:
    files = project_files(run)
    if not files:
        return None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()
