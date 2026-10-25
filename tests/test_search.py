import asyncio
import time

import pytest

from app import search_engine as se
from app.answer import select_answer_sources
from app.config import Settings


def test_normalize_url_dedupes_variants():
    a = se.normalize_url("https://www.Example.com/post/?utm_source=x&id=2#frag")
    b = se.normalize_url("http://example.com/post?id=2")
    assert a == b


def test_relevance_prefers_on_topic():
    q = "latest stable Rust release"
    good = se.relevance(q, "Announcing Rust 1.100.0", "The Rust team released stable version 1.100", "https://blog.rust-lang.org/x")
    bad = se.relevance(q, "Latest Polls", "Latest governor polls 2026", "https://www.nytimes.com/interactive/polls/latest-polls.html")
    assert good > 0.6 and bad < 0.35


def test_domain_quality():
    assert se.domain_quality("expertbeacon.com", "nginx rate limiting") < 0
    assert se.domain_quality("cheap-best-deals-online.xyz", "x") <= -0.25
    assert se.domain_quality("nginx.org", "nginx rate limiting") > 0.2  # authority + official
    assert se.domain_quality("docs.python.org", "python asyncio") > 0


def _item(url, title, content="", engine="ddg"):
    return {"url": url, "title": title, "content": content, "engine": engine}


def test_rank_dedupes_and_caps_per_domain():
    q = "sqlite wal mode concurrency"
    items = [
        _item("https://sqlite.org/wal.html", "Write-Ahead Logging - SQLite", "WAL mode concurrency readers writers"),
        _item("https://www.sqlite.org/wal.html?utm_source=x", "Write-Ahead Logging", "WAL mode concurrency readers writers sqlite", engine="exa"),
        _item("https://a.example.com/1", "SQLite WAL mode concurrency explained", "sqlite wal concurrency"),
        _item("https://a.example.com/2", "SQLite WAL mode concurrency deep dive", "sqlite wal concurrency"),
        _item("https://a.example.com/3", "SQLite WAL mode concurrency pitfalls", "sqlite wal concurrency"),
        _item("https://spam.xyz/x", "SQLite WAL mode concurrency", "sqlite wal concurrency casino"),
        _item("https://news.example.org/polls", "Latest polls", "election polls"),
    ]
    out = se.rank_results(q, items)
    urls = [r["url"] for r in out]
    assert sum("sqlite.org/wal" in u for u in urls) == 1          # dedupe
    assert urls[0].endswith("sqlite.org/wal.html") or "sqlite.org" in urls[0]  # official first
    top5_domains = [r["domain"] for r in out[:5]]
    assert top5_domains.count("a.example.com") <= 2                 # per-domain cap in top slots
    assert urls.index("https://spam.xyz/x") > 2                      # spam demoted
    assert urls[-1] in ("https://news.example.org/polls", "https://a.example.com/3")  # off-topic / overflow last


def test_engines_run_concurrently_and_slow_engine_times_out(monkeypatch):
    def slow_ddg(query, limit, timeout, news=False, timelimit=None):
        time.sleep(0.6 if not news else 5)  # news engine "hangs"
        return [_item("https://x.org/a", "wal mode sqlite", "sqlite wal")]

    async def fake_exa(client, query, limit, api_key, news=False, days=None):
        await asyncio.sleep(0.6)
        return [_item("https://y.org/b", "sqlite wal concurrency", "sqlite wal", engine="exa")]

    monkeypatch.setattr(se, "search_duckduckgo", slow_ddg)
    monkeypatch.setattr(se, "search_exa_async", fake_exa)
    s = Settings(exa_api_key="k", engine_timeout_sec=1.0, request_timeout_sec=20)
    async def timed():
        t = time.perf_counter()
        r = await se.multi_engine_search_async("latest sqlite wal news", 5, s)  # "latest" -> fresh -> ddg-news too
        return r, time.perf_counter() - t

    # (measure inside the loop: asyncio.run() itself waits for the abandoned worker thread on shutdown)
    res, dt = asyncio.run(timed())
    assert dt < 2.5, dt                         # parallel, and the hung engine is cut off
    assert {r["url"] for r in res} == {"https://x.org/a", "https://y.org/b"}


def test_answer_uses_top_on_topic_results_only():
    rs = [{"url": f"u{i}", "relevance": r} for i, r in enumerate([0.9, 0.8, 0.1, 0.7, 0.6, 0.5])]
    picked = select_answer_sources(rs, 4)
    assert [p["url"] for p in picked] == ["u0", "u1", "u3", "u4"]
    assert select_answer_sources([{"url": "a", "relevance": 0.1}], 4) == [{"url": "a", "relevance": 0.1}]
