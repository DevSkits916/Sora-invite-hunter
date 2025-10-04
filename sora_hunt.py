"""Sora Invite Code Hunter web application - Enhanced version."""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional
from urllib.parse import quote_plus

import requests
from flask import Flask, jsonify, render_template_string

# Configuration defaults

DEFAULT_QUERY = "Sora invite code OR 'Sora 2 invite' OR 'Sora2 invite'"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 "
    "(SoraInviteHunter/2.0; +https://github.com/Sora-invite-hunter)"
)
DEFAULT_POLL_INTERVAL = 60
DEFAULT_MAX_POSTS = 75
MAX_LOG_ENTRIES = 500
MAX_CANDIDATES = 1000
REQUEST_TIMEOUT = 30

# Baseline headers that mimic a modern browser. Individual fetchers can
# override or extend these values, but ensuring every outbound request has a
# reasonably complete header set helps avoid 403 "Forbidden" responses from
# sites that aggressively filter generic user-agents or missing headers.
BASE_REQUEST_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9," "application/json;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    # Many services require a Referer header for anti-bot filtering. Using the
    # target URL is a safe default and will be replaced on a per-request basis
    # when appropriate.
    "Referer": "https://www.google.com/",
}

# API Endpoints

REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
REDDIT_SUBREDDIT_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/new.json"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
OPENAI_FORUM_LATEST_URL = "https://community.openai.com/latest.json"
BLUESKY_SEARCH_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"
X_PROXY_PREFIX = "https://r.jina.ai/"
GITHUB_SEARCH_URL = "https://api.github.com/search/issues"
MASTODON_SEARCH_URL = "https://mastodon.social/api/v2/search"

# Enhanced token pattern - supports various formats

TOKEN_PATTERN = re.compile(r"\b[A-Z0-9]{5,12}\b")
INVITE_KEYWORDS = [
    "invite",
    "code",
    "beta",
    "access",
    "key",
    "token",
    "giveaway",
    "sharing",
    "redeem",
    "signup",
]

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """Represents a potential invite code candidate."""

    code: str
    example_text: str
    source_title: str
    url: str
    discovered_at: str
    confidence_score: float = 0.5
    source_type: str = "unknown"


@dataclass
class AppState:
    """Thread-safe application state."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    candidates: deque[Candidate] = field(default_factory=lambda: deque(maxlen=MAX_CANDIDATES))
    seen_codes: set[str] = field(default_factory=set)
    last_poll: Optional[str] = None
    activity_log: deque[Dict[str, str]] = field(default_factory=lambda: deque(maxlen=MAX_LOG_ENTRIES))
    error_count: int = 0
    success_count: int = 0


state = AppState()


class SourceSpec:
    """Definition for a single external source to poll."""

    def __init__(
        self,
        name: str,
        fetcher: Callable[[Dict[str, str]], List[Dict[str, str]]],
        *,
        enabled: bool = True,
        rate_limit_delay: float = 0.0,
    ) -> None:
        self.name = name
        self.fetcher = fetcher
        self.enabled = enabled
        self.rate_limit_delay = rate_limit_delay
        self.last_error: Optional[str] = None
        self.last_success: Optional[str] = None


def _iso_now() -> str:
    """Return current UTC time in ISO format."""

    return datetime.now(timezone.utc).isoformat()


def _log_event(message: str, level: str = "info") -> None:
    """Store activity log message with timestamp."""

    entry = {"timestamp": _iso_now(), "level": level, "message": message}
    with state.lock:
        state.activity_log.append(entry)


def _get_config() -> Dict[str, str | int]:
    """Read configuration from environment variables."""

    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL))
    max_posts = int(os.getenv("MAX_POSTS", DEFAULT_MAX_POSTS))
    query = os.getenv("QUERY", DEFAULT_QUERY)
    user_agent = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
    return {
        "poll_interval": max(10, poll_interval),
        "max_posts": max(1, min(max_posts, 100)),
        "query": query,
        "user_agent": user_agent,
        "github_token": os.getenv("GITHUB_TOKEN", ""),
    }


def _reddit_headers(user_agent: str) -> Dict[str, str]:
    """Generate Reddit-compatible headers."""

    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.reddit.com/",
    }


def _make_request(
    url: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, str | int]] = None,
    *,
    timeout: int = REQUEST_TIMEOUT,
) -> requests.Response:
    """Make HTTP request with retry logic."""

    max_retries = 3
    merged_headers = {**BASE_REQUEST_HEADERS, **(headers or {})}
    # Update the referer to match the destination when it has not been
    # explicitly overridden by the caller. Some providers (notably GitHub and
    # Reddit) prefer to see the request target referenced in the header.
    merged_headers.setdefault("Referer", url)

    for attempt in range(max_retries):
        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as exc:  # pragma: no cover - network failures
            if attempt == max_retries - 1:
                raise
            logger.warning("Request failed (attempt %s/%s): %s", attempt + 1, max_retries, exc)
            time.sleep(2**attempt)
    raise RuntimeError("Unreachable")


def _fetch_reddit_search(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch Reddit posts using the configured search query."""

    params = {
        "q": config["query"],
        "sort": "new",
        "limit": config["max_posts"],
        "restrict_sr": False,
        "t": "day",
    }
    headers = _reddit_headers(config["user_agent"])
    response = _make_request(REDDIT_SEARCH_URL, headers, params)
    payload = response.json()
    items = payload.get("data", {}).get("children", [])

    results: List[Dict[str, str]] = []
    for item in items:
        data = item.get("data", {})
        title = data.get("title", "")
        body = data.get("selftext", "") or ""
        permalink = data.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
        results.append({"title": title, "body": body, "url": url})
    return results


def _fetch_reddit_search_for(query: str, config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch Reddit posts for a specific query."""

    params = {
        "q": query,
        "sort": "new",
        "limit": config["max_posts"],
        "restrict_sr": False,
        "t": "week",
    }
    headers = _reddit_headers(config["user_agent"])
    response = _make_request(REDDIT_SEARCH_URL, headers, params)
    payload = response.json()
    items = payload.get("data", {}).get("children", [])

    results: List[Dict[str, str]] = []
    for item in items:
        data = item.get("data", {})
        title = data.get("title", "")
        body = data.get("selftext", "") or ""
        permalink = data.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
        results.append({"title": title, "body": body, "url": url})
    return results


def _fetch_reddit_subreddit(subreddit: str, config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch newest posts from a specific subreddit."""

    params = {"limit": config["max_posts"]}
    headers = _reddit_headers(config["user_agent"])
    url = REDDIT_SUBREDDIT_URL_TEMPLATE.format(subreddit=subreddit)
    response = _make_request(url, headers, params)
    payload = response.json()
    items = payload.get("data", {}).get("children", [])

    results: List[Dict[str, str]] = []
    for item in items:
        data = item.get("data", {})
        title = data.get("title", "")
        body = data.get("selftext", "") or ""
        permalink = data.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
        results.append({"title": title, "body": body, "url": url})
    return results


def _fetch_x_search(search_url: str, description: str, config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch X/Twitter search results through proxy."""

    proxied_url = f"{X_PROXY_PREFIX}{search_url}"
    headers = {"User-Agent": config["user_agent"]}
    response = _make_request(proxied_url, headers)
    text_content = response.text[:15000]

    return [
        {
            "title": description,
            "body": text_content,
            "url": search_url,
        }
    ]


def _fetch_bluesky_search(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch Bluesky posts mentioning Sora invites."""

    params = {
        "q": "Sora invite code",
        "limit": min(int(config["max_posts"]), 25),
    }
    headers = {"User-Agent": config["user_agent"]}

    try:
        response = _make_request(BLUESKY_SEARCH_URL, headers, params)
        payload = response.json()
        posts = payload.get("posts", [])

        results: List[Dict[str, str]] = []
        for post in posts:
            record = post.get("record", {})
            text = record.get("text", "")
            author = post.get("author", {}).get("handle", "unknown")
            uri = post.get("uri", "")
            url = ""
            if uri:
                url = f"https://bsky.app/profile/{author}/post/{uri.split('/')[-1]}"

            results.append(
                {
                    "title": f"Bluesky post by @{author}",
                    "body": text,
                    "url": url,
                }
            )
        return results
    except Exception as exc:  # pragma: no cover - network failures
        logger.warning("Bluesky search failed: %s", exc)
        return []


def _fetch_github_issues(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Search GitHub issues and discussions for invite codes."""

    query = quote_plus("Sora invite code OR Sora access code")
    params = {
        "q": query,
        "sort": "created",
        "order": "desc",
        "per_page": min(int(config["max_posts"]), 30),
    }

    headers = {"User-Agent": config["user_agent"]}
    if config.get("github_token"):
        headers["Authorization"] = f"token {config['github_token']}"

    try:
        response = _make_request(GITHUB_SEARCH_URL, headers, params)
        payload = response.json()
        items = payload.get("items", [])

        results: List[Dict[str, str]] = []
        for item in items:
            title = item.get("title", "")
            body = item.get("body", "") or ""
            url = item.get("html_url", "")
            results.append({"title": f"GitHub: {title}", "body": body, "url": url})
        return results
    except Exception as exc:  # pragma: no cover - network failures
        logger.warning("GitHub search failed: %s", exc)
        return []


def _fetch_mastodon_search(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Search Mastodon for Sora invite mentions."""

    params = {
        "q": "Sora invite",
        "type": "statuses",
        "limit": min(int(config["max_posts"]), 20),
    }
    headers = {"User-Agent": config["user_agent"]}

    try:
        response = _make_request(MASTODON_SEARCH_URL, headers, params)
        payload = response.json()
        statuses = payload.get("statuses", [])

        results: List[Dict[str, str]] = []
        for status in statuses:
            content = status.get("content", "")
            clean_content = re.sub(r"<[^>]+>", "", content)
            account = status.get("account", {}).get("acct", "unknown")
            url = status.get("url", "")

            results.append(
                {
                    "title": f"Mastodon post by @{account}",
                    "body": clean_content,
                    "url": url,
                }
            )
        return results
    except Exception as exc:  # pragma: no cover - network failures
        logger.warning("Mastodon search failed: %s", exc)
        return []


def _fetch_hacker_news(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch recent Hacker News stories."""

    params = {
        "query": config["query"],
        "tags": "story,comment",
        "hitsPerPage": min(int(config["max_posts"]), 50),
    }
    response = _make_request(HN_SEARCH_URL, {}, params)
    payload = response.json()
    hits = payload.get("hits", [])

    results: List[Dict[str, str]] = []
    for hit in hits:
        title = hit.get("title") or hit.get("story_title") or ""
        body = hit.get("story_text") or hit.get("comment_text") or ""
        url = hit.get("url") or hit.get("story_url") or ""
        if not url and hit.get("objectID"):
            url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        results.append({"title": title, "body": body, "url": url})
    return results


def _fetch_openai_forum(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch latest OpenAI community forum topics."""

    headers = {"User-Agent": config["user_agent"]}
    response = _make_request(OPENAI_FORUM_LATEST_URL, headers)
    payload = response.json()
    topics = payload.get("topic_list", {}).get("topics", [])

    results: List[Dict[str, str]] = []
    for topic in topics[: int(config["max_posts"])]:
        title = topic.get("title", "")
        excerpt = topic.get("excerpt", "")
        slug = topic.get("slug")
        topic_id = topic.get("id")
        url = ""
        if slug and topic_id is not None:
            url = f"https://community.openai.com/t/{slug}/{topic_id}"
        results.append({"title": title, "body": excerpt, "url": url})
    return results


SOURCES: List[SourceSpec] = [
    SourceSpec("Reddit search (configured)", _fetch_reddit_search),
    SourceSpec(
        "Reddit search (Sora invite code)",
        lambda config: _fetch_reddit_search_for("Sora invite code", config),
    ),
    SourceSpec(
        "Reddit search (Sora beta access)",
        lambda config: _fetch_reddit_search_for('"Sora" "beta" "access"', config),
    ),
    SourceSpec("Reddit /r/ChatGPT", lambda config: _fetch_reddit_subreddit("ChatGPT", config)),
    SourceSpec("Reddit /r/OpenAI", lambda config: _fetch_reddit_subreddit("OpenAI", config)),
    SourceSpec("Reddit /r/SoraAI", lambda config: _fetch_reddit_subreddit("SoraAI", config)),
    SourceSpec("Reddit /r/artificial", lambda config: _fetch_reddit_subreddit("artificial", config)),
    SourceSpec(
        "X live (Sora invite code)",
        lambda config: _fetch_x_search(
            "https://x.com/search?q=Sora%20invite%20code&f=live",
            "Live tweets: Sora invite code",
            config,
        ),
        rate_limit_delay=1.0,
    ),
    SourceSpec(
        "X live (#SoraInvite)",
        lambda config: _fetch_x_search(
            "https://x.com/search?q=%23SoraInvite&f=live",
            "Live tweets: #SoraInvite",
            config,
        ),
        rate_limit_delay=1.0,
    ),
    SourceSpec(
        "X live (#SoraAccess)",
        lambda config: _fetch_x_search(
            "https://x.com/search?q=%23SoraAccess&f=live",
            "Live tweets: #SoraAccess",
            config,
        ),
        rate_limit_delay=1.0,
    ),
    SourceSpec("Bluesky search", _fetch_bluesky_search, rate_limit_delay=2.0),
    SourceSpec("GitHub issues", _fetch_github_issues, rate_limit_delay=3.0),
    SourceSpec("Mastodon search", _fetch_mastodon_search, rate_limit_delay=2.0),
    SourceSpec("Hacker News", _fetch_hacker_news),
    SourceSpec("OpenAI Community", _fetch_openai_forum),
]


def _calculate_confidence(text: str, token: str) -> float:
    """Calculate confidence score based on context."""

    text_lower = text.lower()
    score = 0.5

    keyword_count = sum(1 for kw in INVITE_KEYWORDS if kw in text_lower)
    score += min(keyword_count * 0.1, 0.3)

    if "sora" in text_lower:
        score += 0.15

    if any(word in text_lower for word in ["error", "exception", "stack", "debug"]):
        score -= 0.3

    return min(max(score, 0.1), 1.0)


def _extract_tokens(text: str) -> List[str]:
    """Extract candidate tokens from text."""

    uppercase_text = text.upper()
    candidates: List[str] = []

    for token in TOKEN_PATTERN.findall(uppercase_text):
        if any(ch.isdigit() for ch in token):
            if not any(exclude in token for exclude in ["HTTP", "HTML", "JSON", "XML", "HTTPS"]):
                candidates.append(token)

    return list(dict.fromkeys(candidates))


def _build_example_snippet(title: str, body: str, token: str) -> str:
    """Create snippet highlighting the token."""

    combined = f"{title}\n{body}".strip()
    if not combined:
        return html.escape(title or token)

    match = re.search(re.escape(token), combined, re.IGNORECASE)
    if match:
        start = max(match.start() - 60, 0)
        end = min(match.end() + 60, len(combined))
    else:
        start = 0
        end = min(len(combined), 200)

    snippet = combined[start:end].replace("\n", " ").strip()
    pattern = re.compile(re.escape(token), re.IGNORECASE)

    highlighted_parts: List[str] = []
    last_end = 0
    for token_match in pattern.finditer(snippet):
        highlighted_parts.append(html.escape(snippet[last_end:token_match.start()]))
        highlighted_parts.append(f"<mark>{html.escape(token_match.group(0))}</mark>")
        last_end = token_match.end()
    highlighted_parts.append(html.escape(snippet[last_end:]))

    return "".join(highlighted_parts)


def _process_entries(entries: List[Dict[str, str]], source_label: str) -> List[Candidate]:
    """Process entries and register new candidates."""

    new_candidates: List[Candidate] = []
    for entry in entries:
        title = entry.get("title", "")
        body = entry.get("body", "")
        url = entry.get("url", "")
        tokens = _extract_tokens(f"{title}\n{body}")

        for token in tokens:
            with state.lock:
                if token in state.seen_codes:
                    continue
                state.seen_codes.add(token)

            confidence = _calculate_confidence(f"{title}\n{body}", token)
            snippet = _build_example_snippet(title, body, token)
            display_title = title or "Untitled"
            if source_label and source_label not in display_title:
                display_title = f"[{source_label}] {display_title}"

            candidate = Candidate(
                code=token,
                example_text=snippet,
                source_title=display_title,
                url=url,
                discovered_at=_iso_now(),
                confidence_score=confidence,
                source_type=source_label.split()[0].lower() if source_label else "unknown",
            )

            with state.lock:
                state.candidates.append(candidate)

            new_candidates.append(candidate)
            _log_event(
                f"New candidate {token} from {source_label or 'unknown source'} (conf={confidence:.2f})",
                "success",
            )

    return new_candidates


def _poll_sources() -> None:
    """Main polling loop."""

    while True:
        start_time = time.time()
        config = _get_config()

        _log_event(f"Starting poll cycle ({len(SOURCES)} sources)", "info")
        cycle_candidates: List[Candidate] = []

        for source in SOURCES:
            if not source.enabled:
                continue

            try:
                entries = source.fetcher(config)
                new_from_source = _process_entries(entries, source.name)
                cycle_candidates.extend(new_from_source)

                source.last_success = _iso_now()
                source.last_error = None

                with state.lock:
                    state.success_count += 1

                _log_event(
                    f"{source.name}: {len(entries)} item(s), {len(new_from_source)} new",
                    "debug",
                )

                if source.rate_limit_delay > 0:
                    time.sleep(source.rate_limit_delay)

            except Exception as exc:  # pragma: no cover - network failures
                error_msg = f"{source.name}: {exc}"
                logger.exception("%s", error_msg)
                _log_event(error_msg, "error")

                source.last_error = _iso_now()

                with state.lock:
                    state.error_count += 1

        logger.info("Poll completed: %s new candidates", len(cycle_candidates))

        if cycle_candidates:
            _log_event(f"Discovered {len(cycle_candidates)} new candidates", "success")
        else:
            _log_event("No new candidates this cycle", "info")

        with state.lock:
            state.last_poll = _iso_now()

        elapsed = time.time() - start_time
        sleep_for = max(config["poll_interval"] - elapsed, 5)
        time.sleep(sleep_for)


def _start_background_thread() -> None:
    """Start the background polling thread."""

    thread = threading.Thread(target=_poll_sources, name="source-poller", daemon=True)
    thread.start()
    logger.info("Background polling thread started")
    _log_event("System initialized", "info")


@app.route("/")
def index() -> str:
    """Serve the main web interface."""

    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Sora Invite Code Hunter</title>
        <style>
            :root {
                --bg-primary: #f5f5f5;
                --bg-secondary: #fff;
                --text-primary: #222;
                --text-secondary: #555;
                --border-color: #ccc;
                --hover-bg: #f0f0f0;
                --success-color: #064;
                --error-color: #b00;
                --info-color: #0a5;
            }

            @media (prefers-color-scheme: dark) {
                :root {
                    --bg-primary: #1a1a1a;
                    --bg-secondary: #2a2a2a;
                    --text-primary: #e0e0e0;
                    --text-secondary: #b0b0b0;
                    --border-color: #444;
                    --hover-bg: #333;
                }
            }

            * { box-sizing: border-box; }

            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
                margin: 0;
                padding: 2rem;
                background-color: var(--bg-primary);
                color: var(--text-primary);
                line-height: 1.6;
            }

            .container { max-width: 1400px; margin: 0 auto; }

            h1 {
                color: var(--text-primary);
                margin-bottom: 0.5rem;
                font-size: 2rem;
            }

            h2 {
                margin-top: 2.5rem;
                font-size: 1.5rem;
                border-bottom: 2px solid var(--border-color);
                padding-bottom: 0.5rem;
            }

            .stats {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 1rem;
                margin: 1.5rem 0;
            }

            .stat-card {
                background: var(--bg-secondary);
                padding: 1rem;
                border-radius: 8px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }

            .stat-label {
                font-size: 0.85rem;
                color: var(--text-secondary);
                text-transform: uppercase;
                letter-spacing: 0.5px;
            }

            .stat-value {
                font-size: 1.75rem;
                font-weight: bold;
                margin-top: 0.25rem;
            }

            table {
                border-collapse: collapse;
                width: 100%;
                background: var(--bg-secondary);
                box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                border-radius: 8px;
                overflow: hidden;
            }

            th, td {
                border: 1px solid var(--border-color);
                padding: 0.75rem;
                text-align: left;
                vertical-align: top;
            }

            th {
                background: var(--hover-bg);
                font-weight: 600;
                position: sticky;
                top: 0;
                z-index: 10;
            }

            tbody tr:hover { background: var(--hover-bg); }

            code {
                font-size: 1.1rem;
                font-weight: bold;
                font-family: "Courier New", Courier, monospace;
                color: var(--success-color);
            }

            .confidence {
                display: inline-block;
                padding: 0.2rem 0.5rem;
                border-radius: 4px;
                font-size: 0.85rem;
                font-weight: 600;
            }

            .confidence-high { background: #d4edda; color: #155724; }
            .confidence-medium { background: #fff3cd; color: #856404; }
            .confidence-low { background: #f8d7da; color: #721c24; }

            .controls {
                display: flex;
                gap: 1rem;
                align-items: center;
                margin: 1.5rem 0;
                flex-wrap: wrap;
            }

            button {
                padding: 0.6rem 1.2rem;
                border: 1px solid var(--border-color);
                background: var(--bg-secondary);
                color: var(--text-primary);
                border-radius: 6px;
                cursor: pointer;
                font-size: 1rem;
                transition: all 0.2s ease;
            }

            button:hover {
                background: var(--hover-bg);
                transform: translateY(-1px);
            }

            #status {
                font-size: 0.9rem;
                color: var(--text-secondary);
                padding: 0.5rem;
            }

            #activityLog {
                list-style: none;
                padding: 0;
                max-height: 300px;
                overflow-y: auto;
                background: var(--bg-secondary);
                border: 1px solid var(--border-color);
                border-radius: 8px;
            }

            #activityLog li {
                border-bottom: 1px solid var(--border-color);
                padding: 0.75rem;
                font-family: "Courier New", Courier, monospace;
                font-size: 0.9rem;
            }

            #activityLog li:last-child { border-bottom: none; }

            .log-timestamp {
                font-weight: bold;
                margin-right: 0.75rem;
                color: var(--text-secondary);
            }

            .log-info { color: var(--info-color); }
            .log-error { color: var(--error-color); }
            .log-debug { color: var(--text-secondary); }
            .log-success { color: var(--success-color); }

            .empty {
                text-align: center;
                color: var(--text-secondary);
                padding: 2rem;
            }

            .sources {
                display: grid;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                gap: 1rem;
            }

            .source-card {
                background: var(--bg-secondary);
                border: 1px solid var(--border-color);
                border-radius: 8px;
                padding: 0.75rem 1rem;
            }

            .source-card h3 {
                margin: 0 0 0.5rem;
                font-size: 1rem;
            }

            .source-card p {
                margin: 0.25rem 0;
                font-size: 0.85rem;
                color: var(--text-secondary);
            }

            .badge {
                display: inline-block;
                padding: 0.2rem 0.5rem;
                border-radius: 999px;
                font-size: 0.75rem;
                font-weight: 600;
                background: var(--hover-bg);
                color: var(--text-secondary);
            }

            @media (max-width: 768px) {
                body { padding: 1rem; }
                h1 { font-size: 1.5rem; }
                table, th, td { font-size: 0.85rem; }
                .controls { flex-direction: column; align-items: stretch; }
                button { width: 100%; }
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎬 Sora Invite Code Hunter</h1>
            <p>Real-time monitoring of potential Sora invite codes from Reddit, X, Bluesky, GitHub, Mastodon, Hacker News, and the OpenAI forums.</p>

            <div class="controls">
                <button id="refreshButton" type="button">🔄 Refresh now</button>
                <label>
                    <input type="checkbox" id="autoRefresh" checked />
                    Auto refresh every minute
                </label>
                <span id="status">Waiting for first update…</span>
            </div>

            <div class="stats">
                <div class="stat-card">
                    <div class="stat-label">Total Candidates</div>
                    <div class="stat-value" id="totalCandidates">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Unique Codes Seen</div>
                    <div class="stat-value" id="uniqueCodes">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Successful Polls</div>
                    <div class="stat-value" id="successCount">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Errors</div>
                    <div class="stat-value" id="errorCount">0</div>
                </div>
            </div>

            <p><strong>Last Poll:</strong> <span id="lastPoll">not yet</span></p>
            <p><strong>Tracking Query:</strong> <code id="queryDisplay"></code></p>

            <table>
                <thead>
                    <tr>
                        <th>Code</th>
                        <th>Confidence</th>
                        <th>Source</th>
                        <th>Example Text</th>
                        <th>Discovered</th>
                    </tr>
                </thead>
                <tbody id="candidatesBody">
                    <tr><td colspan="5" class="empty">Loading candidates…</td></tr>
                </tbody>
            </table>

            <h2>Source Status</h2>
            <div id="sources" class="sources"></div>

            <h2>Activity Log</h2>
            <ul id="activityLog">
                <li class="empty">Waiting for log entries…</li>
            </ul>
        </div>
        <script>
            const candidatesBody = document.getElementById('candidatesBody');
            const lastPollEl = document.getElementById('lastPoll');
            const statusEl = document.getElementById('status');
            const logEl = document.getElementById('activityLog');
            const refreshButton = document.getElementById('refreshButton');
            const autoRefreshEl = document.getElementById('autoRefresh');
            const totalCandidatesEl = document.getElementById('totalCandidates');
            const uniqueCodesEl = document.getElementById('uniqueCodes');
            const successCountEl = document.getElementById('successCount');
            const errorCountEl = document.getElementById('errorCount');
            const queryDisplayEl = document.getElementById('queryDisplay');
            const sourcesEl = document.getElementById('sources');

            let refreshTimer = null;

            function setStatus(text, isError = false) {
                statusEl.textContent = text;
                statusEl.style.color = isError ? 'var(--error-color)' : 'var(--text-secondary)';
            }

            function escapeHtml(value) {
                return String(value ?? '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }

            function confidenceClass(score) {
                if (score >= 0.75) {
                    return 'confidence confidence-high';
                }
                if (score >= 0.5) {
                    return 'confidence confidence-medium';
                }
                return 'confidence confidence-low';
            }

            function renderCandidates(candidates) {
                if (!candidates.length) {
                    candidatesBody.innerHTML = '<tr><td colspan="5" class="empty">No candidates found yet.</td></tr>';
                    return;
                }

                const rows = candidates.map(item => `
                    <tr>
                        <td>
                            ${item.url ? `<a href="${encodeURI(item.url)}" target="_blank" rel="noopener"><code>${escapeHtml(item.code)}</code></a>` : `<code>${escapeHtml(item.code)}</code>`}
                        </td>
                        <td><span class="${confidenceClass(item.confidence_score)}">${(item.confidence_score * 100).toFixed(0)}%</span></td>
                        <td>${escapeHtml(item.source_title || 'Unknown source')}</td>
                        <td>${item.example_text || ''}</td>
                        <td>${escapeHtml(item.discovered_at || '')}</td>
                    </tr>
                `).join('');
                candidatesBody.innerHTML = rows;
            }

            function renderLog(entries) {
                if (!entries.length) {
                    logEl.innerHTML = '<li class="empty">No activity recorded yet.</li>';
                    return;
                }

                const items = entries.map(entry => {
                    const levelClass = `log-${entry.level}`;
                    return `<li class="${levelClass}"><span class="log-timestamp">${escapeHtml(entry.timestamp)}</span>${escapeHtml(entry.message)}</li>`;
                }).join('');
                logEl.innerHTML = items;
            }

            function renderSources(sources) {
                if (!sources.length) {
                    sourcesEl.innerHTML = '<p class="empty">No sources configured.</p>';
                    return;
                }

                const cards = sources.map(source => {
                    const lastSuccess = source.last_success ? escapeHtml(source.last_success) : 'never';
                    const lastError = source.last_error ? escapeHtml(source.last_error) : '—';
                    const status = source.enabled ? 'Enabled' : 'Disabled';
                    return `
                        <div class="source-card">
                            <h3>${escapeHtml(source.name)}</h3>
                            <p>Status: <span class="badge">${status}</span></p>
                            <p>Last success: ${lastSuccess}</p>
                            <p>Last error: ${lastError}</p>
                        </div>
                    `;
                }).join('');
                sourcesEl.innerHTML = cards;
            }

            async function fetchData(manual = false) {
                try {
                    setStatus(manual ? 'Refreshing…' : 'Updating…');
                    const response = await fetch('/codes.json', { cache: 'no-store' });
                    if (!response.ok) {
                        throw new Error(`Request failed with status ${response.status}`);
                    }
                    const data = await response.json();
                    lastPollEl.textContent = data.last_poll || 'not yet';
                    totalCandidatesEl.textContent = data.total_candidates ?? data.candidates.length;
                    uniqueCodesEl.textContent = data.unique_codes ?? data.candidates.length;
                    successCountEl.textContent = data.success_count ?? 0;
                    errorCountEl.textContent = data.error_count ?? 0;
                    queryDisplayEl.textContent = data.query || '';

                    renderCandidates(data.candidates || []);
                    renderLog(data.activity_log || []);
                    renderSources(data.sources || []);

                    setStatus(manual ? 'Refreshed' : `Last updated at ${new Date().toLocaleTimeString()}`);
                } catch (err) {
                    console.error(err);
                    setStatus(`Error updating: ${err.message}`, true);
                }
            }

            function scheduleRefresh() {
                if (refreshTimer) {
                    clearInterval(refreshTimer);
                    refreshTimer = null;
                }
                if (autoRefreshEl.checked) {
                    refreshTimer = setInterval(fetchData, 60000);
                }
            }

            refreshButton.addEventListener('click', () => fetchData(true));
            autoRefreshEl.addEventListener('change', scheduleRefresh);

            fetchData();
            scheduleRefresh();
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)


@app.route("/codes.json")
def codes_json():
    """Provide JSON snapshot of the current state."""

    config = _get_config()
    with state.lock:
        candidates = [asdict(candidate) for candidate in reversed(state.candidates)]
        activity_log = list(reversed(state.activity_log))
        snapshot = {
            "query": config["query"],
            "poll_interval_seconds": config["poll_interval"],
            "max_posts": config["max_posts"],
            "last_poll": state.last_poll,
            "total_candidates": len(state.candidates),
            "unique_codes": len(state.seen_codes),
            "success_count": state.success_count,
            "error_count": state.error_count,
            "candidates": candidates,
            "activity_log": activity_log,
            "sources": [
                {
                    "name": source.name,
                    "enabled": source.enabled,
                    "last_success": source.last_success,
                    "last_error": source.last_error,
                }
                for source in SOURCES
            ],
        }
    return jsonify(snapshot)


# Start the background worker as soon as the module is imported
_start_background_thread()


def create_app() -> Flask:
    """Factory compatible with WSGI servers."""

    return app


if __name__ == "__main__":
    port = int(os.getenv("PORT", 3000))
    host = os.getenv("HOST", "0.0.0.0")
    app.run(host=host, port=port)
