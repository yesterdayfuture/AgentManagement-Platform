import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "mcp_servers" / "web_search.py"
    spec = importlib.util.spec_from_file_location("web_search_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bing_parser_reads_title_link_and_snippet():
    module = _module()
    parser = module._BingParser()
    parser.feed(
        '<li class="b_algo"><h2><a href="https://example.com/gk">2027国考</a></h2><p>招录35691人</p></li>'
    )
    assert parser.results[0] == {"title": "2027国考", "url": "https://example.com/gk", "snippet": "招录35691人"}


def test_search_drops_unrelated_pages_and_includes_body(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module,
        "_bing",
        lambda query, limit: [
            {"title": "邓小平", "url": "https://zh.wikipedia.org/wiki/A", "snippet": "政治家"},
            {"title": "2027国考职位表", "url": "https://example.com/gk", "snippet": "2027国考招录35691人"},
        ],
    )
    monkeypatch.setattr(module, "_duckduckgo", lambda query, limit: [])

    def excerpt(url, query, limit=900):
        return "2027国考招录35691人，应届生岗位见职位表" if "example.com" in url else ""

    monkeypatch.setattr(module, "_excerpt", excerpt)
    text = module.search_web("2027年国考岗位共有多少")
    assert "35691" in text
    assert "正文：" in text
    assert "邓小平" not in text


def test_search_requires_the_name_not_a_loose_topic(monkeypatch):
    module = _module()
    monkeypatch.setattr(
        module,
        "_bing",
        lambda query, limit: [
            {"title": "僵尸", "url": "https://baike.baidu.com/item/僵尸", "snippet": "中国民间传说的鬼怪"},
            {"title": "林正英", "url": "https://example.com/lam", "snippet": "林正英以僵尸片道长闻名，代表作僵尸先生"},
        ],
    )
    monkeypatch.setattr(module, "_duckduckgo", lambda query, limit: [])
    monkeypatch.setattr(module, "_excerpt", lambda url, query, limit=900: "")
    text = module.search_web("林正英老师都演过哪些僵尸片")
    assert "僵尸先生" in text
    assert "民间传说" not in text


def test_excerpt_skips_private_addresses(monkeypatch):
    module = _module()
    called = {"n": 0}

    def fetch(url, timeout=4):
        called["n"] += 1
        return b"secret"

    monkeypatch.setattr(module, "_fetch", fetch)
    assert module._excerpt("http://127.0.0.1/secret", "国考") == ""
    assert called["n"] == 0
