"""Optional web access for Steve (config.json -> "web").

When enabled, any prompt going into the AI gets augmented:
  - contains a URL           -> the page text is fetched and attached
  - "look up X" / "search X" / "google X" -> DuckDuckGo results attached

Private/LAN addresses are always refused so chat can't make Steve poke
around the local network.
"""

import ipaddress
import re
import socket
import urllib.parse
import urllib.request
from html import unescape

from state import load_config

URL_RE = re.compile(r"https?://[^\s\]>\"']+")
SEARCH_RE = re.compile(r"\b(?:look up|search(?: for| up)?|google)[:\s]+(.{3,200})", re.I)
TAG_RE = re.compile(r"<[^>]+>")
# a real browser UA - DDG serves bot-detection pages to obvious bots
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}


def _cfg():
    return load_config().get("web", {})


def _blocked(url):
    """True if the URL points anywhere private/local."""
    host = urllib.parse.urlparse(url).hostname or ""
    if not host or host.lower() == "localhost":
        return True
    for attempt in (1, 2):      # retry once - transient DNS failure != private
        try:
            for info in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
            return False
        except Exception:
            continue
    return True                 # could not resolve at all - fail closed


def _get(url, timeout, limit=500_000):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        ctype = r.headers.get("Content-Type", "text/html")
        if "text" not in ctype and "json" not in ctype:
            return None
        return r.read(limit).decode("utf-8", "replace")


def _strip_html(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", unescape(TAG_RE.sub(" ", html))).strip()


def fetch_page(url, max_chars, timeout):
    if _blocked(url):
        print(f"[web] blocked private/local url: {url}")
        return None
    html = _get(url, timeout)
    return _strip_html(html)[:max_chars] if html else None


def search_web(query, max_chars, timeout):
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    html = _get(url, timeout)
    if not html:
        return None
    titles = [_strip_html(t) for t in
              re.findall(r"(?s)class='result-link'[^>]*>(.*?)</a>", html)]
    snippets = [_strip_html(s) for s in
                re.findall(r"(?s)class='result-snippet'[^>]*>(.*?)</td>", html)]
    results = [f"- {t}: {s}" for t, s in zip(titles, snippets) if t][:5]
    return "\n".join(results)[:max_chars] or None


def augment(prompt):
    """Return extra web context for this prompt, or None."""
    cfg = _cfg()
    if not cfg.get("enabled", False):
        return None
    max_chars = int(cfg.get("maxChars", 2000))
    timeout = int(cfg.get("timeout", 10))

    m = URL_RE.search(prompt)
    if m:
        url = m.group(0).rstrip(".,)")
        try:
            text = fetch_page(url, max_chars, timeout)
            if text:
                print(f"[web] fetched {url} ({len(text)} chars)")
                return f"[Content of {url}]: {text}"
        except Exception as e:
            print(f"[web] fetch error: {e}")
        return None

    m = SEARCH_RE.search(prompt)
    if m:
        query = m.group(1).strip(" ?!.\"'")
        try:
            results = search_web(query, max_chars, timeout)
            if results:
                print(f"[web] searched '{query}'")
                return f'[Web search results for "{query}"]:\n{results}'
        except Exception as e:
            print(f"[web] search error: {e}")
    return None
