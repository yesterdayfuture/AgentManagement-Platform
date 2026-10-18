"""网上搜索 MCP 服务。协议为 Streamable HTTP 2025-03-26。

启动：在 backend 目录执行
    .venv/bin/python mcp_servers/web_search.py
地址：http://127.0.0.1:8765/mcp
工具：web.search，参数 query（必填）、limit（1 到 8，默认 5）。
先检索必应，打不开时再试 DuckDuckGo。和搜索词对得上的结果会再打开页面，抽出相关正文。
"""

import html
import ipaddress
import json
import os
import re
import socket
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

HOST = os.environ.get("WEB_SEARCH_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("WEB_SEARCH_MCP_PORT", "8765"))
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


class _ResultParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._mode = ""
        self._buf: list[str] = []
        self.results: list[dict] = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "")
        if tag == "a" and "result__a" in classes:
            self._mode = "title"
            self._buf = []
            self.results.append({"title": "", "url": _result_url(dict(attrs).get("href", "")), "snippet": ""})
        elif tag in {"a", "td"} and "result__snippet" in classes:
            self._mode = "snippet"
            self._buf = []

    def handle_endtag(self, tag):
        if self._mode == "title" and tag == "a" and self.results:
            self.results[-1]["title"] = "".join(self._buf).strip()
            self._mode = ""
        elif self._mode == "snippet" and tag in {"a", "td"} and self.results:
            self.results[-1]["snippet"] = "".join(self._buf).strip()
            self._mode = ""

    def handle_data(self, data):
        if self._mode:
            self._buf.append(data)


def _result_url(href: str) -> str:
    if "uddg=" in href:
        raw = parse_qs(urlparse(href).query).get("uddg", [""])[0]
        return unquote(raw)
    if href.startswith("//"):
        return "https:" + href
    return href


def _fetch(url: str, timeout: float = 4) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)


def _plain(html: str) -> str:
    parser = _Text()
    parser.feed(html or "")
    return "".join(parser.parts).strip()


_STOP = set("的了是在与及或和年共有多少哪些没有要求请给出详细分析一下")


def _tokens(query: str) -> list[str]:
    """抽出年份、英文词和连续中文，用来判断结果是否真的在说这件事。"""
    found = []
    for part in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", query or ""):
        if re.fullmatch(r"[A-Za-z0-9]+", part):
            if len(part) >= 2:
                found.append(part.lower())
            continue
        buf: list[str] = []
        for char in part:
            if char in _STOP:
                if len(buf) >= 2:
                    found.append("".join(buf))
                buf = []
            else:
                buf.append(char)
        if len(buf) >= 2:
            found.append("".join(buf))
    return found


def _relevant(query: str, item: dict) -> bool:
    blob = f"{item.get('title') or ''} {item.get('snippet') or ''}".lower()
    tokens = _tokens(query)
    if not tokens:
        return True
    years = re.findall(r"20\d{2}", query or "")
    if years and not any(year in blob for year in years) and not any(token in blob for token in tokens if len(token) >= 2):
        return False
    return any(token in blob for token in tokens)


def _public_http(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_reserved or address.is_unspecified:
            return False
    return True


def _bing(query: str, limit: int) -> list[dict]:
    url = "https://www.bing.com/search?" + urlencode({"q": query, "setlang": "zh-CN"})
    parser = _BingParser()
    parser.feed(_fetch(url, timeout=4).decode("utf-8", "replace"))
    return [item for item in parser.results if item["title"] and item["url"].startswith("http")][:limit]


class _BingParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._algo = 0
        self._h2 = False
        self._link = False
        self._snippet = False
        self._buf: list[str] = []
        self.results: list[dict] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = values.get("class", "")
        if tag == "li" and "b_algo" in classes:
            self._algo += 1
            self.results.append({"title": "", "url": "", "snippet": ""})
        elif self._algo and tag == "h2":
            self._h2 = True
        elif self._algo and self._h2 and tag == "a" and self.results and not self.results[-1]["url"]:
            self._link = True
            self._buf = []
            self.results[-1]["url"] = html.unescape(values.get("href", ""))
        elif self._algo and tag == "p" and self.results and not self.results[-1]["snippet"]:
            self._snippet = True
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "li" and self._algo:
            self._algo -= 1
            self._h2 = False
            self._link = False
            self._snippet = False
        elif tag == "h2":
            self._h2 = False
        elif self._link and tag == "a" and self.results:
            self.results[-1]["title"] = html.unescape("".join(self._buf)).strip()
            self._link = False
        elif self._snippet and tag == "p" and self.results:
            self.results[-1]["snippet"] = html.unescape("".join(self._buf)).strip()
            self._snippet = False

    def handle_data(self, data):
        if self._link or self._snippet:
            self._buf.append(data)


def _excerpt(url: str, query: str, limit: int = 900) -> str:
    if not _public_http(url):
        return ""
    raw = _fetch(url, timeout=2.5)
    page = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw.decode("utf-8", "replace"))
    text = re.sub(r"\s+", " ", _plain(page)).strip()
    if not text:
        return ""
    index = -1
    for token in _tokens(query):
        found = text.lower().find(token.lower())
        if found >= 0 and (index < 0 or found < index):
            index = found
    start = 0 if index < 0 else max(0, index - 80)
    return text[start : start + limit]


def _wikipedia(query: str, limit: int) -> list[dict]:
    url = "https://zh.wikipedia.org/w/api.php?" + urlencode(
        {"action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json", "utf8": 1}
    )
    payload = json.loads(_fetch(url).decode("utf-8", "replace"))
    rows = []
    for item in (payload.get("query") or {}).get("search") or []:
        title = item.get("title") or ""
        rows.append(
            {
                "title": title,
                "url": "https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_")),
                "snippet": _plain(item.get("snippet") or ""),
            }
        )
    return rows


def _duckduckgo(query: str, limit: int) -> list[dict]:
    url = "https://html.duckduckgo.com/html/?" + urlencode({"q": query})
    parser = _ResultParser()
    parser.feed(_fetch(url).decode("utf-8", "replace"))
    return [item for item in parser.results if item["title"]][:limit]


def _dedupe(rows: list[dict]) -> list[dict]:
    seen = set()
    kept = []
    for item in rows:
        key = (item.get("url") or "").split("?", 1)[0]
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(item)
    return kept


def search_web(query: str, limit: int = 5) -> str:
    text = (query or "").strip()
    if not text:
        raise ValueError("请填写搜索词")
    if len(text) > 200:
        raise ValueError("搜索词过长")
    count = max(1, min(int(limit or 5), 8))
    rows = []
    errors = []
    try:
        rows.extend(_bing(text, count))
    except Exception as exc:
        errors.append(f"必应暂时不可用：{exc.__class__.__name__}")
    if len(rows) < count:
        try:
            rows.extend(_duckduckgo(text, count))
        except Exception as exc:
            errors.append(f"网页暂时不可用：{exc.__class__.__name__}")
    matched = [item for item in _dedupe(rows) if _relevant(text, item)]
    if not matched:
        try:
            matched = [item for item in _wikipedia(text, count) if _relevant(text, item)]
        except Exception as exc:
            errors.append(f"维基百科暂时不可用：{exc.__class__.__name__}")
    matched = matched[:count]
    if not matched:
        if errors:
            raise RuntimeError("；".join(errors))
        return "没有找到和搜索词对应的结果。"
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {id(item): pool.submit(_excerpt, item["url"], text) for item in matched[:3]}
        for item in matched[:3]:
            try:
                item["excerpt"] = jobs[id(item)].result(timeout=3)
            except (FuturesTimeout, Exception):
                item["excerpt"] = ""
    lines = []
    for item in matched:
        line = f"- {item['title']}：{item['snippet']} {item['url']}"
        excerpt = (item.get("excerpt") or "").strip()
        if excerpt:
            line += f"\n  正文：{excerpt}"
        lines.append(line)
    return "网页\n" + "\n".join(lines)


def _tool_result(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if urlparse(self.path).path != "/mcp":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "请求不是 JSON"}}, 400)
            return
        method = body.get("method")
        session = self.headers.get("Mcp-Session-Id") or "web-search"
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("mcp-session-id", session)
            self.end_headers()
            return
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "web-search", "version": "1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "web.search",
                        "description": "按关键词搜索公开网页，返回标题、摘要、链接和相关正文。一次问题最多搜几条不同的词；拿到相关结果后直接回答，不要反复换词再搜。",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string", "description": "搜索词"},
                                "limit": {"type": "integer", "description": "返回条数，1 到 8，默认 5"},
                            },
                            "required": ["query"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = body.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name != "web.search":
                result = _tool_result(f"未知工具 {name}", True)
            else:
                try:
                    result = _tool_result(search_web(str(arguments.get("query") or ""), arguments.get("limit") or 5))
                except (ValueError, RuntimeError) as exc:
                    result = _tool_result(str(exc), True)
        else:
            self._send({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": "方法不存在"}}, 200, session)
            return
        self._send({"jsonrpc": "2.0", "id": body.get("id"), "result": result}, 200, session)

    def _send(self, payload: dict, status: int, session: str = "web-search") -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("mcp-session-id", session)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        return


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"web-search mcp http://{HOST}:{PORT}/mcp", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
