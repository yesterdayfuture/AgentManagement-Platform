"""把上传文件变成可检索的文本块。

支持 txt、md、csv、json、html、docx，以及带文字层的 PDF。扫描版 PDF 抽不出文字。
切分策略有 auto、heading、paragraph、sentence、fixed。auto 只是建议一种策略，
真正切开时按调用方选定的策略。标题切分保持每一节独立，不把短节合并回去。
"""

import html
import io
import re
import zipfile
from xml.etree import ElementTree as ET

STRATEGIES = ("auto", "heading", "paragraph", "sentence", "fixed")

STRATEGY_LABELS = {
    "auto": "自动",
    "heading": "按标题",
    "paragraph": "按段落",
    "sentence": "按句子",
    "fixed": "固定长度",
}


class IngestError(ValueError):
    pass


def strategy_catalog() -> list[dict]:
    return [{"value": key, "label": STRATEGY_LABELS[key]} for key in STRATEGIES]


def extract_text(filename: str, data: bytes) -> tuple[str, str]:
    if not data:
        raise IngestError("文件是空的")
    if len(data) > 8 * 1024 * 1024:
        raise IngestError("文件超过 8MB")
    name = (filename or "upload.txt").rsplit("/", 1)[-1]
    ext = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if ext == "docx" or (data[:2] == b"PK" and ext not in {"xlsx", "pptx", "zip"}):
        return _docx(data), "docx"
    if ext == "pdf" or data.startswith(b"%PDF-"):
        return _pdf(data), "pdf"
    if ext in {"html", "htm"}:
        return _html(data), "html"
    kind = {"markdown": "md"}.get(ext, ext or "text")
    if ext and ext not in {"txt", "md", "markdown", "csv", "tsv", "json", "log", "xml", "yml", "yaml"}:
        raise IngestError("只接受 txt、md、csv、json、html、docx、pdf")
    text = _decode(data)
    if "\x00" in text:
        raise IngestError("这个文件不是可解析的文本")
    text = text.strip()
    if not text:
        raise IngestError("没有解析出文字")
    return text, kind


def suggest_strategy(text: str) -> str:
    sample = text[:20000]
    if re.search(r"(?m)^#{1,6}\s+\S", sample) or re.search(r"(?m)^第[0-9一二三四五六七八九十百]+[章节部分篇]", sample):
        return "heading"
    lines = [line.strip() for line in sample.splitlines() if line.strip()]
    if len(lines) >= 3 and sum(len(line) for line in lines) / len(lines) <= 80:
        return "paragraph"
    if len(re.findall(r"[。！？!?]", sample)) >= 2:
        return "sentence"
    return "fixed"


def split_text(content: str, strategy: str = "paragraph", chunk_size: int = 500, chunk_overlap: int = 40) -> tuple[list[str], str]:
    text = (content or "").strip()
    if not text:
        raise IngestError("没有可切分的正文")
    chosen = strategy or "paragraph"
    if chosen not in STRATEGIES:
        raise IngestError("未知的切分策略")
    applied = suggest_strategy(text) if chosen == "auto" else chosen
    size, overlap = _limits(chunk_size, chunk_overlap, applied)
    if applied == "heading":
        pieces = _by_heading(text, size, overlap)
    elif applied == "paragraph":
        pieces = _pack(_blocks(text), size, overlap)
    elif applied == "sentence":
        pieces = _pack(_sentences(text), size, overlap)
    else:
        pieces = _windows(text, size, overlap)
    pieces = [piece for piece in pieces if piece.strip()]
    if not pieces:
        raise IngestError("没有可切分的正文")
    return pieces, applied


def _limits(chunk_size: int, chunk_overlap: int, applied: str) -> tuple[int, int]:
    defaults = {"heading": 800, "paragraph": 800, "sentence": 400, "fixed": 500}
    try:
        size = int(chunk_size or 0)
        overlap = int(chunk_overlap or 0)
    except (TypeError, ValueError) as exc:
        raise IngestError("切分长度必须是数字") from exc
    if size <= 0:
        size = defaults[applied]
    size = min(max(size, 40), 4000)
    if overlap < 0:
        overlap = 0
    if overlap >= size:
        raise IngestError("重叠长度必须小于切块长度")
    return size, overlap


def _blocks(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    return parts or [text]


def _sentences(text: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"(?<=[。！？!?；;])\s+|\n+", text) if part.strip()]
    return parts or [text]


def _by_heading(text: str, size: int, overlap: int) -> list[str]:
    pattern = re.compile(r"(?m)^(#{1,6}\s+\S.*|第[0-9一二三四五六七八九十百]+[章节部分篇].*)$")
    marks = list(pattern.finditer(text))
    if not marks:
        return _pack(_blocks(text), size, overlap)
    sections = []
    if marks[0].start() > 0:
        lead = text[: marks[0].start()].strip()
        if lead:
            sections.append(lead)
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        section = text[mark.start() : end].strip()
        if section:
            sections.append(section)
    pieces = []
    for section in sections:
        pieces.extend(_windows(section, size, overlap) if len(section) > size else [section])
    return pieces


def _pack(parts: list[str], size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for part in parts:
        if len(part) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_windows(part, size, overlap))
            continue
        candidate = f"{current}\n\n{part}".strip() if current else part
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = part
    if current:
        chunks.append(current)
    return chunks


def _windows(text: str, size: int, overlap: int) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= size:
        return [text] if text else []
    step = max(1, size - overlap)
    pieces = []
    index = 0
    while index < len(text):
        piece = text[index : index + size].strip()
        if piece:
            pieces.append(piece)
        if index + size >= len(text):
            break
        index += step
    return pieces


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _html(data: bytes) -> str:
    text = _decode(data)
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|h[1-6]|li|tr)>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise IngestError("没有解析出文字")
    return text


def _docx(data: bytes) -> str:
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            xml = package.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise IngestError("docx 无法打开") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise IngestError("docx 正文损坏") from exc
    paragraphs = []
    for paragraph in root.iter(f"{namespace}p"):
        line = "".join(node.text or "" for node in paragraph.iter(f"{namespace}t")).strip()
        if line:
            paragraphs.append(line)
    text = "\n\n".join(paragraphs).strip()
    if not text:
        raise IngestError("没有解析出文字")
    return text


def _pdf(data: bytes) -> str:
    import zlib

    blobs = []
    for match in re.finditer(rb"stream\r?\n(.*?)\n?endstream", data, re.S):
        raw = match.group(1).strip(b"\r\n")
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        blobs.append(raw)
    source = b"\n".join(blobs) if blobs else data
    pieces = []
    for match in re.finditer(rb"\((?:\\.|[^\\)])*\)\s*Tj", source, re.S):
        literal = match.group(0)
        end = literal.rfind(b")")
        pieces.append(_pdf_literal(literal[1:end]))
    for match in re.finditer(rb"<([0-9A-Fa-f\s]+)>\s*Tj", source):
        pieces.append(_pdf_hex(match.group(1)))
    for match in re.finditer(rb"\[(.*?)\]\s*TJ", source, re.S):
        for literal in re.finditer(rb"\((?:\\.|[^\\)])*\)", match.group(1)):
            raw = literal.group(0)
            pieces.append(_pdf_literal(raw[1:-1]))
    text = re.sub(r"[ \t]+\n", "\n", "".join(pieces))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise IngestError("没有从 PDF 里抽出文字。扫描件需要先做文字识别。")
    return text


def _pdf_literal(raw: bytes) -> str:
    out = bytearray()
    index = 0
    while index < len(raw):
        if raw[index] == 0x5C and index + 1 < len(raw):
            nxt = raw[index + 1]
            mapped = {ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("("): b"(", ord(")"): b")", ord("\\"): b"\\"}
            if nxt in mapped:
                out.extend(mapped[nxt])
                index += 2
                continue
            if nxt in b"01234567":
                octal = bytearray([nxt])
                index += 2
                for _ in range(2):
                    if index < len(raw) and raw[index] in b"01234567":
                        octal.append(raw[index])
                        index += 1
                    else:
                        break
                out.append(int(octal, 8) & 0xFF)
                continue
        out.append(raw[index])
        index += 1
    return _decode(bytes(out))


def _pdf_hex(raw: bytes) -> str:
    digits = re.sub(rb"\s+", b"", raw)
    if len(digits) % 2:
        digits += b"0"
    try:
        data = bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return ""
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="ignore")
    return _decode(data)
