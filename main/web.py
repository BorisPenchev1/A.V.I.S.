"""Internet access for AVIS: web search and readable page fetching.

Both tools use only the standard library. `web_search` needs no API key (it
queries DuckDuckGo's HTML endpoints) but will use Brave Search when
``BRAVE_API_KEY`` is set. `web_fetch` refuses private/local addresses so the
model cannot be steered into requesting internal services (e.g. the Ollama
port) through a crafted URL.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import urllib.parse
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


USER_AGENT = os.getenv(
    "AVIS_WEB_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
FETCH_TIMEOUT = float(os.getenv("AVIS_WEB_TIMEOUT", "12"))
MAX_FETCH_BYTES = int(os.getenv("AVIS_WEB_MAX_BYTES", "400000"))
# Keep fetched text small: it is fed back into a modest local context window, so
# a huge page would overflow it, slow generation, and risk a model timeout.
MAX_TEXT_CHARS = int(os.getenv("AVIS_WEB_MAX_CHARS", "5000"))
MAX_RESULTS = int(os.getenv("AVIS_WEB_MAX_RESULTS", "5"))


# --- helpers ---------------------------------------------------------------

def _http_get(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> tuple[bytes, str, str, str]:
    request = Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urlopen(request, timeout=FETCH_TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read(MAX_FETCH_BYTES + 1)
        return raw, charset, response.geturl(), (response.headers.get_content_type() or "")


def _html_to_text(html: str) -> str:
    """Strip HTML to readable text without any third-party parser."""
    html = re.sub(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1>", " ", html)
    html = re.sub(r"(?is)<head\b.*?</head>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article|header|footer)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean_inline(html: str) -> str:
    return re.sub(r"\s+", " ", _html_to_text(html)).strip()


def _guard_public_url(url: str) -> urllib.parse.ParseResult:
    """Allow only public http(s) URLs; block private/loopback/link-local hosts."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http and https URLs can be fetched.")
    host = parsed.hostname
    if not host:
        raise ValueError("That URL has no host.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        raise RuntimeError(f"Could not resolve {host}.") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise PermissionError("Refusing to fetch a private or local network address.")
    return parsed


# --- search providers ------------------------------------------------------

def _brave_search(query: str, key: str, max_results: int) -> list[tuple[str, str, str]]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": max_results}
    )
    raw, charset, _final, _ctype = _http_get(
        url, headers={"X-Subscription-Token": key, "Accept": "application/json"}
    )
    data = json.loads(raw.decode(charset, errors="replace"))
    results = []
    for item in data.get("web", {}).get("results", [])[:max_results]:
        results.append((item.get("title", "").strip(), item.get("url", "").strip(),
                        _clean_inline(item.get("description", ""))))
    return results


def _ddg_unwrap(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.path.startswith("/l/") or "duckduckgo.com/l/" in href:
        query = urllib.parse.parse_qs(parsed.query)
        if "uddg" in query:
            return query["uddg"][0]
    return href


def _ddg_search(query: str, max_results: int) -> list[tuple[str, str, str]]:
    """Scrape DuckDuckGo's no-JS endpoints (html, then lite)."""
    encoded = urllib.parse.urlencode({"q": query})
    attempts = [
        ("https://html.duckduckgo.com/html/", encoded.encode()),   # POST
        ("https://lite.duckduckgo.com/lite/", encoded.encode()),   # POST
    ]
    for url, body in attempts:
        try:
            raw, charset, _final, _ctype = _http_get(
                url, headers={"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"}, data=body
            )
        except (HTTPError, URLError, OSError):
            continue
        html = raw.decode(charset, errors="replace")
        results: list[tuple[str, str, str]] = []
        # html endpoint: result anchors carry class="result__a"
        anchors = re.findall(r'(?is)<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html)
        snippets = re.findall(r'(?is)class="result__snippet"[^>]*>(.*?)</a>', html)
        if anchors:
            for index, (href, title) in enumerate(anchors[:max_results]):
                snippet = _clean_inline(snippets[index]) if index < len(snippets) else ""
                results.append((_clean_inline(title), _ddg_unwrap(href), snippet))
            if results:
                return results
        # lite endpoint: plain result links in a table
        lite = re.findall(r'(?is)<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html)
        if not lite:
            lite = re.findall(r'(?is)<a[^>]+href="(https?://[^"]+|//duckduckgo\.com/l/[^"]+)"[^>]*>(.*?)</a>', html)
        for href, title in lite:
            target = _ddg_unwrap(href)
            text = _clean_inline(title)
            if target.startswith("http") and text and "duckduckgo.com" not in target:
                results.append((text, target, ""))
            if len(results) >= max_results:
                break
        if results:
            return results
    return []


# --- tools -----------------------------------------------------------------

def web_search(query: str, max_results: int = MAX_RESULTS) -> str:
    """Search the web and return the top results as title, URL, and snippet."""
    if not query or not query.strip():
        raise ValueError("A search query is required.")
    query = query.strip()
    try:
        count = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        count = MAX_RESULTS

    key = os.getenv("BRAVE_API_KEY")
    results: list[tuple[str, str, str]] = []
    try:
        if key:
            results = _brave_search(query, key, count)
        if not results:
            results = _ddg_search(query, count)
    except HTTPError as error:
        raise RuntimeError(f"Web search failed with HTTP {error.code}.") from error
    except (TimeoutError, socket.timeout) as error:
        raise RuntimeError(f"The search request timed out after {int(FETCH_TIMEOUT)}s.") from error
    except (URLError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise RuntimeError(f"Could not reach the search service: {reason}") from error

    if not results:
        return f"No web results found for {query!r}."
    lines = [f"Web results for {query!r}:"]
    for index, (title, url, snippet) in enumerate(results, 1):
        entry = f"{index}. {title or url}\n   {url}"
        if snippet:
            entry += f"\n   {snippet}"
        lines.append(entry)
    lines.append("\nUse web_fetch on a URL above to read the full page.")
    return "\n".join(lines)


def web_fetch(url: str) -> str:
    """Fetch a public web page and return its readable text (bounded)."""
    if not url or not url.strip():
        raise ValueError("A URL is required.")
    url = url.strip()
    if not re.match(r"(?i)^[a-z][a-z0-9+.-]*://", url):
        url = "https://" + url
    _guard_public_url(url)
    try:
        raw, charset, final_url, content_type = _http_get(url, headers={"Accept": "text/html,*/*"})
    except HTTPError as error:
        raise RuntimeError(f"The page returned HTTP {error.code}.") from error
    except (TimeoutError, socket.timeout) as error:
        raise RuntimeError(f"Fetching {url} timed out after {int(FETCH_TIMEOUT)}s.") from error
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise RuntimeError(f"Fetching {url} timed out after {int(FETCH_TIMEOUT)}s.") from error
        raise RuntimeError(f"Could not reach {url}: {error.reason}") from error
    except OSError as error:
        raise RuntimeError(f"Could not reach {url}: {error}") from error

    truncated = len(raw) > MAX_FETCH_BYTES
    body = raw[:MAX_FETCH_BYTES].decode(charset, errors="replace")
    if "html" in content_type or re.search(r"(?is)<html", body[:2000]):
        text = _html_to_text(body)
    else:
        text = body.strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rstrip() + "\n…[truncated]"
    elif truncated:
        text += "\n…[truncated]"
    if not text:
        return f"Fetched {final_url} but found no readable text."
    return f"Source: {final_url}\n\n{text}"
