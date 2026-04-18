#!/usr/bin/env python3
"""Extract stock mentions from an article and summarize Yahoo Finance history."""

from __future__ import annotations

import argparse
import calendar
import dataclasses
import datetime as dt
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Iterable


DATE_PATTERNS = [
    re.compile(r"(?P<year>20\d{2}|19\d{2})[/-](?P<month>0?[1-9]|1[0-2])[/-](?P<day>0?[1-9]|[12]\d|3[01])"),
    re.compile(r"(?P<year>20\d{2}|19\d{2})年\s*(?P<month>0?[1-9]|1[0-2])月\s*(?P<day>0?[1-9]|[12]\d|3[01])日?"),
    re.compile(r"(?P<year>20\d{2}|19\d{2})[/-](?P<month>0?[1-9]|1[0-2])"),
    re.compile(r"(?P<year>20\d{2}|19\d{2})年\s*(?P<month>0?[1-9]|1[0-2])月"),
]

TW_NUMERIC_STOCK = re.compile(r"(?<![A-Za-z0-9])(?P<code>[1-9]\d{3})(?![A-Za-z0-9])(?:\s*(?:\.|－|-)?\s*[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]*)?")
EXPLICIT_YAHOO_SYMBOL = re.compile(r"(?<![A-Za-z0-9.])(?P<symbol>[A-Z0-9]{1,10}(?:\.(?:TW|TWO|HK|T|SS|SZ)))(?![A-Za-z0-9.])")
CASHTAG_OR_US_TICKER = re.compile(r"(?<![A-Za-z0-9.])(?:\$)?(?P<symbol>[A-Z]{1,5})(?![A-Za-z0-9])")

COMMON_WORDS = {
    "A",
    "AI",
    "ABF",
    "API",
    "CEO",
    "CFO",
    "EPS",
    "ETF",
    "GDP",
    "HTML",
    "HTTP",
    "IPO",
    "JSON",
    "JS",
    "NASDAQ",
    "NYSE",
    "OTC",
    "Q",
    "QQ",
    "QOQ",
    "ROE",
    "TWSE",
    "URL",
    "XD",
    "YOY",
}


@dataclasses.dataclass(frozen=True)
class StockMention:
    raw: str
    yahoo_symbol: str
    market: str
    name: str


@dataclasses.dataclass(frozen=True)
class DateRange:
    start: dt.date
    end: dt.date
    source: str


@dataclasses.dataclass(frozen=True)
class DateMention:
    date: dt.date
    precision: str


@dataclasses.dataclass(frozen=True)
class TwStockInfo:
    code: str
    name: str
    market: str
    yahoo_symbol: str


@dataclasses.dataclass(frozen=True)
class AccountSection:
    account: str
    text: str


class StockScribeError(RuntimeError):
    pass


TWSE_LISTED_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_LISTED_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"
_TW_REGISTRY: dict[str, TwStockInfo] | None = None
_ARTICLE_PARSE_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_ARTICLE_PARSE_CACHE_LOCK = threading.Lock()


class StockScribe:
    def __init__(self, *, user_agent: str = "StockScribe/1.0") -> None:
        self.user_agent = user_agent

    def snapshot_article(
        self,
        article: str,
        *,
        start: str | None = None,
        end: str | None = None,
        market: str = "auto",
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        _emit_progress(progress, stage="dates", current=0, total=1, message="判讀文章時間區間")
        date_range = resolve_date_range(article, start=start, end=end)
        _emit_progress(progress, stage="extract", current=0, total=1, message="辨識文章提到的股票")
        mentions = extract_stock_mentions(article, market=market)
        return self._snapshot_mentions(article, mentions, date_range=date_range, progress=progress)

    def _snapshot_mentions(
        self,
        article: str,
        mentions: list[StockMention],
        *,
        date_range: DateRange,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        histories = {}
        errors = {}
        total = len(mentions)
        if total == 0:
            _emit_progress(progress, stage="done", current=1, total=1, message="沒有辨識到股票")
        for index, mention in enumerate(mentions, start=1):
            _emit_progress(
                progress,
                stage="history",
                current=index - 1,
                total=total,
                message=f"查詢 {mention.name}（{mention.yahoo_symbol}）歷史資料",
            )
            try:
                histories[mention.yahoo_symbol] = self.fetch_yahoo_history(
                    mention.yahoo_symbol,
                    date_range.start,
                    date_range.end,
                )
            except StockScribeError as exc:
                histories[mention.yahoo_symbol] = []
                errors[mention.yahoo_symbol] = str(exc)
            _emit_progress(
                progress,
                stage="history",
                current=index,
                total=total,
                message=f"完成 {mention.name}（{mention.yahoo_symbol}）",
            )

        _emit_progress(progress, stage="summary", current=total, total=total, message="整理賺賠摘要")
        summaries = []
        for mention in mentions:
            rows = histories[mention.yahoo_symbol]
            summary = summarize_history(
                mention,
                rows,
                requested_start=date_range.start,
                requested_end=date_range.end,
            )
            if mention.yahoo_symbol in errors:
                summary["error"] = errors[mention.yahoo_symbol]
            summaries.append(summary)

        return {
            "snapshot_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "date_range": dataclasses.asdict(date_range),
            "stocks": [dataclasses.asdict(mention) for mention in mentions],
            "histories": histories,
            "summaries": summaries,
            "errors": errors,
        }

    def snapshot_url(
        self,
        url: str,
        *,
        start: str | None = None,
        end: str | None = None,
        market: str = "auto",
        force_refresh: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        cache_key = (url.strip(), market)
        cached = None
        if not force_refresh:
            with _ARTICLE_PARSE_CACHE_LOCK:
                cached = _ARTICLE_PARSE_CACHE.get(cache_key)

        if cached is None:
            message = "讀取文章網址（強制重新搜尋）" if force_refresh else "讀取文章網址"
            _emit_progress(progress, stage="fetch", current=0, total=1, message=message)
            article, account_sections = fetch_article_text_and_accounts(url, user_agent=self.user_agent)
            _emit_progress(progress, stage="fetch", current=1, total=1, message="文章讀取完成")
            _emit_progress(progress, stage="extract", current=0, total=1, message="辨識文章提到的股票")
            mentions = extract_stock_mentions(article, market=market)
            mention_accounts = map_stock_mention_accounts([dataclasses.asdict(mention) for mention in mentions], account_sections)
            cached = {
                "article": article,
                "mentions": mentions,
                "mention_accounts": mention_accounts,
                "cached_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            with _ARTICLE_PARSE_CACHE_LOCK:
                _ARTICLE_PARSE_CACHE[cache_key] = cached
            cache_hit = False
        else:
            _emit_progress(progress, stage="cache", current=1, total=1, message="使用文章股票快取")
            cache_hit = True

        article = cached["article"]
        mentions = cached["mentions"]
        mention_accounts = cached["mention_accounts"]
        _emit_progress(progress, stage="dates", current=0, total=1, message="判讀文章時間區間")
        date_range = resolve_date_range(article, start=start, end=end)
        snapshot = self._snapshot_mentions(article, mentions, date_range=date_range, progress=progress)
        for stock in snapshot["stocks"]:
            stock["mentioned_by"] = mention_accounts.get(stock["yahoo_symbol"], [])
        for summary in snapshot["summaries"]:
            summary["mentioned_by"] = mention_accounts.get(summary["symbol"], [])
        snapshot["source_url"] = url
        snapshot["article_excerpt"] = article[:1200]
        snapshot["article_cache"] = {
            "hit": cache_hit,
            "forced": force_refresh,
            "cached_at": cached.get("cached_at"),
            "key": {"url": cache_key[0], "market": cache_key[1]},
        }
        return snapshot

    def fetch_yahoo_history(self, symbol: str, start: dt.date, end: dt.date) -> list[dict[str, Any]]:
        # Yahoo period2 is exclusive, so add one day to include the requested end date.
        period1 = int(time.mktime(dt.datetime.combine(start, dt.time.min).timetuple()))
        period2 = int(time.mktime(dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min).timetuple()))
        query = urllib.parse.urlencode(
            {
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "history",
                "includeAdjustedClose": "true",
            }
        )
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent})

        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise StockScribeError(f"Yahoo Finance returned HTTP {exc.code} for {symbol}") from exc
        except urllib.error.URLError as exc:
            raise StockScribeError(f"Could not reach Yahoo Finance for {symbol}: {exc.reason}") from exc
        except http.client.IncompleteRead as exc:
            raise StockScribeError(f"Yahoo Finance connection ended early for {symbol}; please retry.") from exc

        chart = payload.get("chart", {})
        error = chart.get("error")
        if error:
            raise StockScribeError(f"Yahoo Finance error for {symbol}: {error.get('description', error)}")

        result = (chart.get("result") or [None])[0]
        if not result:
            return []

        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        adjclose = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []

        rows: list[dict[str, Any]] = []
        for index, timestamp in enumerate(timestamps):
            close = _at(quote.get("close"), index)
            if close is None:
                continue
            rows.append(
                {
                    "date": dt.datetime.fromtimestamp(timestamp).date().isoformat(),
                    "open": _round(_at(quote.get("open"), index)),
                    "high": _round(_at(quote.get("high"), index)),
                    "low": _round(_at(quote.get("low"), index)),
                    "close": _round(close),
                    "adjclose": _round(_at(adjclose, index)),
                    "volume": _at(quote.get("volume"), index),
                }
            )
        return rows


def extract_stock_mentions(article: str, *, market: str = "auto") -> list[StockMention]:
    mentions: dict[str, StockMention] = {}
    registry = get_tw_stock_registry()

    for match in EXPLICIT_YAHOO_SYMBOL.finditer(article):
        raw = match.group("symbol").upper()
        mentions[raw] = StockMention(raw=raw, yahoo_symbol=raw, market="explicit", name=raw)

    if market in {"auto", "tw", "twse", "tpex"}:
        for name, info in find_tw_name_mentions(article, registry):
            if market in {"tw", "twse"} and info.market != "twse":
                continue
            if market == "tpex" and info.market != "tpex":
                continue
            mentions.setdefault(info.yahoo_symbol, StockMention(raw=name, yahoo_symbol=info.yahoo_symbol, market=info.market, name=info.name))

    for match in TW_NUMERIC_STOCK.finditer(article):
        code = match.group("code")
        code_start, code_end = match.span("code")
        if _looks_like_year_near_date(article, code_start, code_end):
            continue
        if _looks_like_price_or_amount_context(article, code_start, code_end):
            continue
        info = registry.get(code)
        if info is None and not _has_stock_code_context(article, code_start, code_end):
            continue
        yahoo_symbol = info.yahoo_symbol if info else f"{code}.TW" if market in {"auto", "tw", "twse"} else code
        if market == "tpex":
            yahoo_symbol = info.yahoo_symbol if info and info.market == "tpex" else f"{code}.TWO"
        if market in {"tw", "twse"} and info and info.market != "twse":
            continue
        if market == "tpex" and info and info.market != "tpex":
            continue
        mentions.setdefault(yahoo_symbol, StockMention(raw=code, yahoo_symbol=yahoo_symbol, market=info.market if info else "tw", name=info.name if info else code))

    for match in CASHTAG_OR_US_TICKER.finditer(article):
        raw = match.group("symbol").upper()
        is_cashtag = match.group(0).startswith("$")
        if raw in COMMON_WORDS or raw in mentions:
            continue
        if not is_cashtag and len(raw) == 1 and market != "us":
            continue
        if not is_cashtag and market not in {"us", "auto"}:
            continue
        mentions.setdefault(raw, StockMention(raw=raw, yahoo_symbol=raw, market="us", name=raw))

    return sorted(mentions.values(), key=lambda item: item.yahoo_symbol)


def get_tw_stock_registry() -> dict[str, TwStockInfo]:
    global _TW_REGISTRY
    if _TW_REGISTRY is not None:
        return _TW_REGISTRY

    registry: dict[str, TwStockInfo] = {}
    registry.update(_fetch_tw_registry_source(TWSE_LISTED_URL, market="twse", code_key="公司代號", name_keys=("公司簡稱", "公司名稱")))
    registry.update(
        _fetch_tw_registry_source(
            TPEX_LISTED_URL,
            market="tpex",
            code_key="SecuritiesCompanyCode",
            name_keys=("CompanyAbbreviation", "CompanyName"),
        )
    )
    _TW_REGISTRY = registry
    return registry


def find_tw_name_mentions(article: str, registry: dict[str, TwStockInfo]) -> list[tuple[str, TwStockInfo]]:
    found: dict[str, tuple[str, TwStockInfo]] = {}
    name_items = [
        (name, info)
        for name, info in registry.items()
        if not name.isdigit() and len(name) >= 2 and name in article
    ]
    for name, info in sorted(name_items, key=lambda item: len(item[0]), reverse=True):
        found.setdefault(info.yahoo_symbol, (name, info))
    return list(found.values())


def _fetch_tw_registry_source(
    url: str,
    *,
    market: str,
    code_key: str,
    name_keys: tuple[str, ...],
) -> dict[str, TwStockInfo]:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "StockScribe/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            rows = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, http.client.IncompleteRead):
        return {}

    suffix = ".TW" if market == "twse" else ".TWO"
    registry: dict[str, TwStockInfo] = {}
    for row in rows:
        code = str(row.get(code_key) or "").strip()
        if not re.fullmatch(r"[1-9]\d{3}", code):
            continue
        names_in_order = [_normalize_tw_company_name(str(row.get(key) or "").strip()) for key in name_keys]
        names = {name for name in names_in_order if name}
        display_name = next((name for name in names_in_order if name), code)
        info = TwStockInfo(code=code, name=display_name, market=market, yahoo_symbol=f"{code}{suffix}")
        registry[code] = info
        for name in names:
            if len(name) >= 2:
                registry.setdefault(name, info)
    return registry


def _normalize_tw_company_name(name: str) -> str:
    name = name.replace("　", "").strip()
    name = name.replace("(股)", "").replace("（股）", "")
    for suffix in ("股份有限公司", "有限公司", "公司"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def fetch_article_text(url: str, *, user_agent: str = "StockScribe/1.0") -> str:
    text, _account_sections = fetch_article_text_and_accounts(url, user_agent=user_agent)
    return text


def fetch_article_text_and_accounts(url: str, *, user_agent: str = "StockScribe/1.0") -> tuple[str, list[AccountSection]]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise StockScribeError("URL must start with http:// or https://.")

    try:
        content_type, charset, raw = fetch_url_bytes(url, user_agent=user_agent)
    except urllib.error.HTTPError as exc:
        raise StockScribeError(f"Article URL returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise StockScribeError(f"Could not fetch article URL: {exc.reason}") from exc
    except http.client.IncompleteRead as exc:
        raise StockScribeError(f"Could not fetch article URL: connection ended early after {len(exc.partial)} bytes. Please retry.") from exc

    text = raw.decode(charset, errors="replace")
    account_sections: list[AccountSection] = []
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        account_sections = extract_account_sections_from_html(text)
        text = html_to_text(text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise StockScribeError("Article URL did not contain readable text.")
    return text, account_sections


def fetch_url_bytes(url: str, *, user_agent: str, attempts: int = 3) -> tuple[str, str, bytes]:
    last_error: urllib.error.URLError | urllib.error.HTTPError | http.client.IncompleteRead | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=article_request_headers(url, user_agent=user_agent))
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                content_type = response.headers.get("Content-Type", "")
                charset = response.headers.get_content_charset() or "utf-8"
                return content_type, charset, response.read()
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, http.client.IncompleteRead) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.7 * attempt)
    if last_error:
        raise last_error
    raise urllib.error.URLError("unknown network error")


def article_request_headers(url: str, *, user_agent: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    browser_user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 StockScribe/1.0"
    )
    headers = {
        "User-Agent": browser_user_agent if user_agent == "StockScribe/1.0" else user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        "Cache-Control": "no-cache",
        "Connection": "close",
    }
    if parsed.netloc.endswith("ptt.cc"):
        headers["Cookie"] = "over18=1"
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/bbs/Stock/index.html"
    return headers


def extract_account_sections_from_html(markup: str) -> list[AccountSection]:
    sections = extract_ptt_account_sections(markup)
    return sections


def extract_ptt_account_sections(markup: str) -> list[AccountSection]:
    if "bbs/Stock" not in markup and "push-userid" not in markup:
        return []

    sections: list[AccountSection] = []
    author = _extract_ptt_author(markup)
    main_match = re.search(
        r'<span class="article-meta-tag">時間</span><span class="article-meta-value">[^<]*</span></div>(?P<body>.*?)(?:<span class="f2">※ 發信站|<div class="push">)',
        markup,
        re.DOTALL,
    )
    if author and main_match:
        main_text = re.sub(r"\s+", " ", html_to_text(main_match.group("body"))).strip()
        if main_text:
            sections.append(AccountSection(account=author, text=main_text))

    for push_match in re.finditer(r'<div class="push">(?P<body>.*?)</div>', markup, re.DOTALL):
        body = push_match.group("body")
        user_match = re.search(r'<span class="[^"]*push-userid[^"]*">(?P<user>.*?)</span>', body, re.DOTALL)
        content_match = re.search(r'<span class="[^"]*push-content[^"]*">(?P<content>.*?)</span>', body, re.DOTALL)
        if not user_match or not content_match:
            continue
        account = strip_html_fragment(user_match.group("user")).strip()
        content = strip_html_fragment(content_match.group("content")).lstrip(":").strip()
        content = re.sub(r"\s+", " ", content)
        if account and content:
            sections.append(AccountSection(account=account, text=content))
    return sections


def strip_html_fragment(fragment: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", fragment))


def map_stock_mention_accounts(stocks: list[dict[str, Any]], sections: list[AccountSection]) -> dict[str, list[str]]:
    mention_accounts: dict[str, list[str]] = {str(stock["yahoo_symbol"]): [] for stock in stocks}
    for stock in stocks:
        symbol = str(stock["yahoo_symbol"])
        accounts = []
        for section in sections:
            if _section_mentions_stock(stock, section.text) and section.account not in accounts:
                accounts.append(section.account)
        mention_accounts[symbol] = accounts
    return mention_accounts


def _section_mentions_stock(stock: dict[str, Any], text: str) -> bool:
    symbol = str(stock.get("yahoo_symbol") or "")
    variants = {
        str(stock.get("raw") or "").strip(),
        str(stock.get("name") or "").strip(),
        symbol,
        symbol.split(".", 1)[0],
    }
    variants = {variant for variant in variants if variant}
    for variant in variants:
        if re.fullmatch(r"[A-Za-z]{1,5}", variant):
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(variant)}(?![A-Za-z0-9])", text, re.IGNORECASE):
                return True
        elif variant in text:
            return True
    return False


def _extract_ptt_author(markup: str) -> str | None:
    match = re.search(r'<span class="article-meta-tag">作者</span><span class="article-meta-value">(?P<author>.*?)</span>', markup, re.DOTALL)
    if not match:
        return None
    author = html_to_text(match.group("author")).strip()
    return re.split(r"\s|\(", author, maxsplit=1)[0] or None


def html_to_text(markup: str) -> str:
    parser = ReadableTextParser()
    parser.feed(markup)
    parser.close()
    return parser.text()


class ReadableTextParser(HTMLParser):
    BLOCK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }
    SKIP_TAGS = {"script", "style", "noscript", "svg"}
    SKIP_CLASSES = {"push-userid", "push-ipdatetime"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.skip_tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = set((dict(attrs).get("class") or "").split())
        if tag in self.SKIP_TAGS or classes.intersection(self.SKIP_CLASSES):
            self.skip_depth += 1
            self.skip_tag_stack.append(tag)
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_tag_stack and self.skip_tag_stack[-1] == tag:
            self.skip_tag_stack.pop()
            self.skip_depth -= 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        cleaned = data.strip()
        if cleaned:
            self.parts.append(unescape(cleaned))

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", " ".join(self.parts))


def resolve_date_range(article: str, *, start: str | None = None, end: str | None = None) -> DateRange:
    today = dt.date.today()
    if start or end:
        start_date = parse_date(start) if start else None
        end_date = parse_date(end, end_of_period=True) if end else today
        if start_date is None:
            start_date = min(extract_dates(article), default=end_date)
        return _validated_range(start_date, end_date, "provided")

    mentions = extract_date_mentions(article)
    if len(mentions) >= 2:
        start_mention = min(mentions, key=lambda mention: mention.date)
        end_mention = max(mentions, key=lambda mention: mention.date)
        return _validated_range(start_mention.date, _end_of_mention(end_mention), "article")
    if len(mentions) == 1:
        return _validated_range(mentions[0].date, today, "article_start_to_today")
    return _validated_range(today - dt.timedelta(days=30), today, "default_last_30_days")


def extract_dates(article: str) -> list[dt.date]:
    return sorted({mention.date for mention in extract_date_mentions(article)})


def extract_date_mentions(article: str) -> list[DateMention]:
    mentions: list[DateMention] = []
    occupied: list[range] = []
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(article):
            if any(match.start() in item or match.end() - 1 in item for item in occupied):
                continue
            year = int(match.group("year"))
            month = int(match.group("month"))
            has_day = bool(match.groupdict().get("day"))
            day = int(match.groupdict().get("day") or 1)
            try:
                mentions.append(DateMention(dt.date(year, month, day), "day" if has_day else "month"))
                occupied.append(range(match.start(), match.end()))
            except ValueError:
                continue
    return sorted(mentions, key=lambda mention: mention.date)


def parse_date(value: str | None, *, end_of_period: bool = False) -> dt.date | None:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m"):
        try:
            parsed = dt.datetime.strptime(value, fmt).date()
            if fmt in {"%Y-%m", "%Y/%m"}:
                day = calendar.monthrange(parsed.year, parsed.month)[1] if end_of_period else 1
                return parsed.replace(day=day)
            return parsed
        except ValueError:
            pass
    raise StockScribeError(f"Invalid date: {value}. Use YYYY-MM-DD, YYYY/MM/DD, YYYY-MM, or YYYY/MM.")


def summarize_history(
    mention: StockMention,
    rows: list[dict[str, Any]],
    *,
    requested_start: dt.date,
    requested_end: dt.date,
) -> dict[str, Any]:
    if not rows:
        return {
            "symbol": mention.yahoo_symbol,
            "raw": mention.raw,
            "name": mention.name,
            "market": mention.market,
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "status": "no_data",
        }

    first = rows[0]
    last = rows[-1]
    start_close = float(first["close"])
    end_close = float(last["close"])
    change = end_close - start_close
    pct_change = (change / start_close * 100) if start_close else None
    closes = [float(row["close"]) for row in rows if row.get("close") is not None]
    volumes = [int(row["volume"]) for row in rows if row.get("volume") is not None]

    return {
        "symbol": mention.yahoo_symbol,
        "raw": mention.raw,
        "name": mention.name,
        "market": mention.market,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "actual_start": first["date"],
        "actual_end": last["date"],
        "trading_days": len(rows),
        "start_close": _round(start_close),
        "end_close": _round(end_close),
        "change": _round(change),
        "pct_change": _round(pct_change),
        "highest_close": _round(max(closes)),
        "lowest_close": _round(min(closes)),
        "average_close": _round(sum(closes) / len(closes)),
        "total_volume": sum(volumes),
        "status": "ok",
    }


def _emit_progress(progress: Callable[[dict[str, Any]], None] | None, **payload: Any) -> None:
    if progress:
        progress(payload)


def _validated_range(start: dt.date, end: dt.date, source: str) -> DateRange:
    if start > end:
        raise StockScribeError(f"Start date {start.isoformat()} is after end date {end.isoformat()}.")
    return DateRange(start=start, end=end, source=source)


def _end_of_mention(mention: DateMention) -> dt.date:
    if mention.precision == "month":
        return mention.date.replace(day=calendar.monthrange(mention.date.year, mention.date.month)[1])
    return mention.date


def _looks_like_year_near_date(article: str, start: int, end: int) -> bool:
    window = article[max(0, start - 2) : min(len(article), end + 3)]
    code = article[start:end]
    if re.fullmatch(r"(?:19|20)\d{2}", code):
        before = article[max(0, start - 8) : start]
        after = article[end : min(len(article), end + 8)]
        if before.endswith(("/", "-")) or after.startswith(("/", "-", "年")):
            return True
        if re.search(r"\d{1,2}/\d{1,2}/$", before):
            return True
        if re.match(r"\s+\d{1,2}:\d{2}", after):
            return True
    return bool(re.search(r"(?:19|20)\d{2}[/-]|(?:19|20)\d{2}年", window))


def _has_stock_code_context(article: str, start: int, end: int) -> bool:
    before = article[max(0, start - 12) : start]
    after = article[end : min(len(article), end + 12)]
    context = before + article[start:end] + after
    if re.search(r"(?:股票|代號|股號|標的|買|賣|持有|推薦|觀察|看好|佈局|布局)", context):
        return True
    if re.search(r"(?<!\d)[1-9]\d{3}\s*(?:\.TW|\.TWO)", context, re.IGNORECASE):
        return True
    if re.search(r"[,、/]\s*[1-9]\d{3}|[1-9]\d{3}\s*[,、/]", context):
        return True
    return False


def _looks_like_price_or_amount_context(article: str, start: int, end: int) -> bool:
    before = article[max(0, start - 12) : start]
    after = article[end : min(len(article), end + 12)]
    if re.match(r"\s*(?:元|塊|以上|以下|上|下|多|點|萬|億|%)", after):
        return True
    if re.search(r"(?:->|-->|＞|>|到|從|破|摸|衝到|上看)\s*$", before):
        return True
    if re.search(r"\d\s*(?:上|到|->|-->|＞|>)\s*$", before):
        return True
    return False


def _at(values: Iterable[Any] | None, index: int) -> Any:
    if values is None:
        return None
    values_list = list(values)
    return values_list[index] if index < len(values_list) else None


def _round(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract stocks from an article and snapshot Yahoo Finance history.")
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--article", help="Article text.")
    input_group.add_argument("--article-file", help="Path to a UTF-8 article text file.")
    parser.add_argument("--start", help="Start date, e.g. 2024-01-01.")
    parser.add_argument("--end", help="End date, e.g. 2024-12-31. Defaults to today when --start is used.")
    parser.add_argument("--market", choices=["auto", "tw", "twse", "tpex", "us"], default="auto")
    parser.add_argument("--output", help="Optional path to write the snapshot JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    article = args.article
    if args.article_file:
        with open(args.article_file, "r", encoding="utf-8") as handle:
            article = handle.read()

    try:
        snapshot = StockScribe().snapshot_article(
            article or "",
            start=args.start,
            end=args.end,
            market=args.market,
        )
    except StockScribeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    output = json.dumps(snapshot, ensure_ascii=False, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(output + "\n")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
