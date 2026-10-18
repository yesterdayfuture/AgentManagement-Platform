"""从技能压缩包读取说明和脚本。

包的结构和 Cursor、Codex 的技能目录一致：优先读 SKILL.md 的开头信息，
脚本只收下 Python、JavaScript 和 Java，路径不允许跳出压缩包。
"""

import io
import re
import zipfile
from pathlib import Path

SUFFIXES = {".py", ".js", ".mjs", ".java"}
_KEY = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_skill_zip(raw: bytes) -> dict:
    """读出名称、标识、说明和最多 8 个脚本。不落盘，也不执行。

    名称优先取 SKILL.md 开头的 name，其次取一级标题，再次取所在目录名。
    标识由名称转成小写短横线。脚本优先取 scripts/ 目录，同名文件只留第一份。
    """
    if len(raw) > 2_000_000:
        raise ValueError("压缩包不能超过 2MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ValueError("压缩包无法打开") from exc
    entries = []
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/").lstrip("/")
        parts = Path(name).parts
        # ".." 和以点开头的目录都能把文件写到技能目录外面，这里直接拒绝整包。
        if not name or name.startswith("/") or ".." in parts or any(part.startswith(".") for part in parts):
            raise ValueError("压缩包路径不合法")
        if len(entries) >= 200:
            raise ValueError("压缩包里的文件过多")
        total += info.file_size
        if info.file_size > 1_000_000 or total > 4_000_000:
            raise ValueError("压缩包解压后过大")
        data = archive.read(info)
        if len(data) > 1_000_000:
            raise ValueError("压缩包解压后过大")
        entries.append((name, data))
    document = _markdown(entries)
    if document is None:
        raise ValueError("压缩包里没有 Markdown 说明")
    path, data = document
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("说明文档不是 UTF-8 文本") from exc
    meta, body = _frontmatter(text)
    folder = Path(path).parent.name
    title = meta.get("name") or _heading(body) or folder or "未命名技能"
    description = meta.get("description") or _lead(body)
    key = _key(meta.get("name") or folder or Path(path).stem)
    scripts = _scripts(entries)
    return {
        "name": title.strip()[:120] or "未命名技能",
        "key": key,
        "description": description.strip()[:4000],
        "body": body.strip()[:100000],
        "scripts": scripts,
    }


def _markdown(entries: list[tuple[str, bytes]]) -> tuple[str, bytes] | None:
    """优先最短路径上的 SKILL.md。没有时才用其他 Markdown。"""
    docs = [(name, data) for name, data in entries if name.lower().endswith(".md")]
    if not docs:
        return None
    named = [item for item in docs if Path(item[0]).name.lower() == "skill.md"]
    pool = named or docs
    return sorted(pool, key=lambda item: (len(Path(item[0]).parts), item[0]))[0]


def _frontmatter(text: str) -> tuple[dict, str]:
    """拆开 Cursor / Codex 技能文件开头的 --- 信息块。没有信息块时全文都是正文。"""
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    end = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if end is None:
        return {}, text.strip()
    return _yaml(lines[1:end]), "\n".join(lines[end + 1:]).strip()


def _yaml(lines: list[str]) -> dict:
    """只解析单行 key: value，以及 >、| 开头的折叠多行。不执行 YAML 标签。"""
    meta = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line[:1].isspace() or ":" not in line:
            index += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if value in {">", "|", ">-", "|-", ">+", "|+"}:
            chunks = []
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
                chunks.append(lines[index].strip())
                index += 1
            meta[key] = " ".join(part for part in chunks if part)
            continue
        meta[key] = value.strip("\"'")
        index += 1
    return meta


def _heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _lead(body: str) -> str:
    chunks = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if chunks:
                break
            continue
        chunks.append(stripped)
    return " ".join(chunks)[:4000]


def _key(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")
    slug = slug[:64].strip("-")
    if not _KEY.fullmatch(slug):
        raise ValueError("说明里的名称不能转成技能标识")
    return slug


def _scripts(entries: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    found = []
    seen = set()
    ordered = sorted(entries, key=lambda item: (0 if "scripts" in Path(item[0]).parts else 1, item[0]))
    for name, data in ordered:
        if Path(name).suffix.lower() not in SUFFIXES:
            continue
        filename = Path(name).name
        if filename in seen:
            continue
        seen.add(filename)
        try:
            source = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{filename} 不是 UTF-8 文本") from exc
        found.append((filename, source))
        if len(found) >= 8:
            break
    return found
