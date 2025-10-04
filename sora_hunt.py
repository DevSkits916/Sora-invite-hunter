"""Sora Invite Code Hunter web application."""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import requests
from flask import Flask, jsonify, render_template_string

# Configuration defaults
DEFAULT_QUERY = "Sora invite code OR 'Sora 2 invite' OR 'Sora2 invite'"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 "
    "(SoraInviteHunter/1.0; +https://github.com/Sora-invite-hunter)"
)
DEFAULT_POLL_INTERVAL = 60
DEFAULT_MAX_POSTS = 75

REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
REDDIT_SUBREDDIT_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/new.json"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
OPENAI_FORUM_LATEST_URL = "https://community.openai.com/latest.json"
X_PROXY_PREFIX = "https://r.jina.ai/"
TOKEN_PATTERN = re.compile(r"\b[A-Z0-9]{5,8}\b")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Shared state guarded by a lock
_state_lock = threading.Lock()
_candidates: List[Dict[str, str]] = []
_seen_codes: set[str] = set()
_last_poll: Optional[str] = None
_activity_log: List[Dict[str, str]] = []


class SourceSpec:
    """Definition for a single external source to poll."""

    def __init__(self, name: str, fetcher: Callable[[Dict[str, str | int]], List[Dict[str, str]]]):
        self.name = name
        self.fetcher = fetcher


def _reddit_headers(user_agent: str) -> Dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


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
    }


def _fetch_reddit_search(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch Reddit posts using the configured search query."""
    params = {
        "q": config["query"],
        "sort": "new",
        "limit": config["max_posts"],
        "restrict_sr": False,
    }
    headers = _reddit_headers(config["user_agent"])
    response = requests.get(REDDIT_SEARCH_URL, params=params, headers=headers, timeout=20)
    response.raise_for_status()
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
    """Fetch Reddit posts for an explicit query string."""
    params = {
        "q": query,
        "sort": "new",
        "limit": config["max_posts"],
        "restrict_sr": False,
    }
    headers = _reddit_headers(config["user_agent"])
    response = requests.get(REDDIT_SEARCH_URL, params=params, headers=headers, timeout=20)
    response.raise_for_status()
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
    """Fetch the newest posts from a specific subreddit."""
    params = {"limit": config["max_posts"]}
    headers = _reddit_headers(config["user_agent"])
    url = REDDIT_SUBREDDIT_URL_TEMPLATE.format(subreddit=subreddit)
    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
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
    """Fetch public X/Twitter search results through a read-only proxy."""
    proxied_url = f"{X_PROXY_PREFIX}{search_url}"
    headers = {"User-Agent": config["user_agent"]}
    response = requests.get(proxied_url, headers=headers, timeout=20)
    response.raise_for_status()
    # The proxy returns the rendered text content, which we can scan directly.
    text_content = response.text
    if len(text_content) > 15000:
        text_content = text_content[:15000]
    return [
        {
            "title": description,
            "body": text_content,
            "url": search_url,
        }
    ]


def _fetch_hacker_news(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch recent Hacker News stories mentioning the search terms."""
    params = {
        "query": config["query"],
        "tags": "story",
        "hitsPerPage": min(int(config["max_posts"]), 50),
    }
    response = requests.get(HN_SEARCH_URL, params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()
    hits = payload.get("hits", [])
    results: List[Dict[str, str]] = []
    for hit in hits:
        title = hit.get("title") or hit.get("story_title") or ""
        body = hit.get("story_text") or hit.get("comment_text") or ""
        url = hit.get("url") or hit.get("story_url") or ""
        if not url and hit.get("objectID"):
            url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
        results.append({"title": title, "body": body or "", "url": url})
    return results


def _fetch_openai_forum(config: Dict[str, str | int]) -> List[Dict[str, str]]:
    """Fetch latest OpenAI community forum topics."""
    headers = {"User-Agent": config["user_agent"]}
    response = requests.get(OPENAI_FORUM_LATEST_URL, headers=headers, timeout=20)
    response.raise_for_status()
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
        "Reddit search (Sora beta code)",
        lambda config: _fetch_reddit_search_for('"Sora" "beta" "code"', config),
    ),
    SourceSpec(
        "Reddit /r/ChatGPT",
        lambda config: _fetch_reddit_subreddit("ChatGPT", config),
    ),
    SourceSpec(
        "Reddit /r/OpenAI",
        lambda config: _fetch_reddit_subreddit("OpenAI", config),
    ),
    SourceSpec(
        "Reddit /r/SoraAI",
        lambda config: _fetch_reddit_subreddit("SoraAI", config),
    ),
    SourceSpec(
        "X live search (Sora invite code)",
        lambda config: _fetch_x_search(
            "https://x.com/search?q=Sora%20invite%20code&f=live",
            "Live tweets mentioning 'Sora invite code'",
            config,
        ),
    ),
    SourceSpec(
        "X live search (#SoraInvite)",
        lambda config: _fetch_x_search(
            "https://x.com/search?q=%23SoraInvite&f=live",
            "Live tweets for #SoraInvite",
            config,
        ),
    ),
    SourceSpec("Hacker News search", _fetch_hacker_news),
    SourceSpec("OpenAI Community latest", _fetch_openai_forum),
]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_event(message: str, level: str = "info") -> None:
    """Store an activity log message with timestamp."""
    entry = {"timestamp": _iso_now(), "level": level, "message": message}
    with _state_lock:
        _activity_log.append(entry)
        if len(_activity_log) > 200:
            del _activity_log[:-200]


def _extract_tokens(text: str) -> List[str]:
    """Extract candidate tokens from text using the defined pattern."""
    uppercase_text = text.upper()
    candidates = []
    for token in TOKEN_PATTERN.findall(uppercase_text):
        if any(ch.isdigit() for ch in token):
            candidates.append(token)
    return candidates


def _build_example_snippet(title: str, body: str, token: str) -> str:
    """Create a short snippet from the source text highlighting the token."""
    combined = f"{title}\n{body}".strip()
    if not combined:
        return title or token

    match = re.search(re.escape(token), combined, re.IGNORECASE)
    if not match:
        snippet = combined[:160]
    else:
        start = max(match.start() - 40, 0)
        end = min(match.end() + 40, len(combined))
        snippet = combined[start:end]
    snippet = snippet.replace("\n", " ")
    return html.escape(snippet.strip())


def _process_entries(entries: List[Dict[str, str]], source_label: str) -> List[Dict[str, str]]:
    """Process fetched entries and register any new invite code candidates."""
    new_candidates: List[Dict[str, str]] = []
    for entry in entries:
        title = entry.get("title", "")
        body = entry.get("body", "")
        url = entry.get("url", "")
        tokens = _extract_tokens(f"{title}\n{body}")

        for token in tokens:
            with _state_lock:
                if token in _seen_codes:
                    continue
            snippet = _build_example_snippet(title, body, token)
            display_title = title or "Untitled"
            if source_label and source_label not in display_title:
                display_title = f"[{source_label}] {display_title}"
            candidate = {
                "code": token,
                "example_text": snippet,
                "source_title": display_title,
                "url": url,
                "discovered_at": _iso_now(),
            }
            with _state_lock:
                _seen_codes.add(token)
                _candidates.append(candidate)
            new_candidates.append(candidate)
            _log_event(
                f"New candidate {token} from {source_label or 'unknown source'}",
                "success",
            )
    return new_candidates


def _poll_sources() -> None:
    """Continuously poll configured sources for invite codes."""
    global _last_poll
    while True:
        start_time = time.time()
        config = _get_config()
        logging.debug(
            "Polling %d sources with interval=%s",
            len(SOURCES),
            config["poll_interval"],
        )
        _log_event(
            f"Polling {len(SOURCES)} sources for invite codes",
            "info",
        )
        cycle_new_candidates: List[Dict[str, str]] = []
        for source in SOURCES:
            try:
                entries = source.fetcher(config)
                new_from_source = _process_entries(entries, source.name)
                cycle_new_candidates.extend(new_from_source)
                _log_event(
                    f"{source.name}: processed {len(entries)} item(s)",
                    "debug",
                )
            except Exception as exc:  # pylint: disable=broad-except
                logging.exception("Error while polling %s: %s", source.name, exc)
                _log_event(f"Error while polling {source.name}: {exc}", "error")

        logging.info(
            "Poll completed: %d new candidate(s) found",
            len(cycle_new_candidates),
        )
        if cycle_new_candidates:
            _log_event(
                f"Discovered {len(cycle_new_candidates)} new candidate(s)",
                "success",
            )
        else:
            _log_event("No new candidates found this cycle", "info")

        with _state_lock:
            _last_poll = _iso_now()
        _log_event("Polling cycle finished", "debug")

        elapsed = time.time() - start_time
        sleep_for = max(config["poll_interval"] - elapsed, 5)
        time.sleep(sleep_for)


def _start_background_thread() -> None:
    thread = threading.Thread(target=_poll_sources, name="source-poller", daemon=True)
    thread.start()
    logging.info("Background source polling thread started")
    _log_event("Background polling thread initialized", "debug")


@app.route("/")
def index() -> str:
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <title>Sora Invite Code Hunter</title>
        <style>
            :root { color-scheme: light dark; }
            body { font-family: Arial, sans-serif; margin: 2rem; background-color: #f5f5f5; color: #222; }
            h1 { color: #333; margin-bottom: 0.25rem; }
            h2 { margin-top: 2rem; }
            table { border-collapse: collapse; width: 100%; background: #fff; box-shadow: 0 0 10px rgba(0,0,0,0.05); }
            th, td { border: 1px solid #ccc; padding: 0.5rem; text-align: left; vertical-align: top; }
            th { background: #eee; }
            tbody tr:nth-child(odd) { background: #fafafa; }
            code { font-size: 1.1rem; font-weight: bold; }
            .timestamp { white-space: nowrap; }
            .controls { display: flex; gap: 1rem; align-items: center; margin: 1rem 0; }
            button { padding: 0.4rem 0.8rem; border: 1px solid #888; background: #fff; border-radius: 4px; cursor: pointer; transition: background 0.2s ease; }
            button:hover { background: #f0f0f0; }
            #status { font-size: 0.9rem; color: #555; }
            #activityLog { list-style: none; padding: 0; max-height: 260px; overflow-y: auto; background: #fff; border: 1px solid #ccc; border-radius: 4px; }
            #activityLog li { border-bottom: 1px solid #eee; padding: 0.5rem; font-family: "Courier New", Courier, monospace; }
            #activityLog li:last-child { border-bottom: none; }
            .log-timestamp { font-weight: bold; margin-right: 0.5rem; }
            .log-info { color: #0a5; }
            .log-error { color: #b00; }
            .log-debug { color: #555; }
            .log-success { color: #064; }
            .empty { text-align: center; color: #777; }
            @media (max-width: 768px) {
                body { margin: 1rem; }
                table, th, td { font-size: 0.9rem; }
                .controls { flex-direction: column; align-items: flex-start; gap: 0.5rem; }
                button { width: 100%; }
            }
        </style>
    </head>
    <body>
        <h1>Sora Invite Code Hunter</h1>
        <p>Tracking the latest potential invite codes shared across Reddit, X, Hacker News, and the OpenAI Community forum. Data refreshes automatically every minute or on demand.</p>
        <div class="controls">
            <button id="refreshButton" type="button">🔄 Refresh candidates</button>
            <span id="status">Waiting for first update…</span>
        </div>
        <p><strong>Last Poll:</strong> <span id="lastPoll">loading…</span></p>
        <table>
            <thead>
                <tr>
                    <th>Code</th>
                    <th>Example Text</th>
                    <th>Source Title</th>
                    <th>Discovered At</th>
                </tr>
            </thead>
            <tbody id="candidatesBody">
                <tr><td colspan="4" class="empty">Loading candidates…</td></tr>
            </tbody>
        </table>
        <h2>Activity Log</h2>
        <ul id="activityLog">
            <li class="empty">Waiting for log entries…</li>
        </ul>
        <script>
            const candidatesBody = document.getElementById('candidatesBody');
            const lastPollEl = document.getElementById('lastPoll');
            const statusEl = document.getElementById('status');
            const logEl = document.getElementById('activityLog');
            const refreshButton = document.getElementById('refreshButton');

            function setStatus(text, isError = false) {
                statusEl.textContent = text;
                statusEl.style.color = isError ? '#b00' : '#555';
            }

            function escapeHtml(value) {
                return String(value ?? '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }

            function renderCandidates(candidates) {
                if (!candidates.length) {
                    candidatesBody.innerHTML = '<tr><td colspan="4" class="empty">No candidates found yet. Check back soon!</td></tr>';
                    return;
                }

                const rows = candidates.map(item => `
                    <tr>
                        <td><a href="${encodeURI(item.url || '#')}" target="_blank" rel="noopener"><code>${escapeHtml(item.code)}</code></a></td>
                        <td>${item.example_text}</td>
                        <td>${escapeHtml(item.source_title || 'Unknown source')}</td>
                        <td class="timestamp">${escapeHtml(item.discovered_at)}</td>
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

            async function fetchData(manual = false) {
                try {
                    setStatus(manual ? 'Refreshing…' : 'Updating…');
                    const response = await fetch('/codes.json', { cache: 'no-store' });
                    if (!response.ok) {
                        throw new Error(`Request failed with status ${response.status}`);
                    }
                    const data = await response.json();
                    lastPollEl.textContent = data.last_poll || 'not yet';
                    renderCandidates(data.candidates || []);
                    renderLog(data.activity_log || []);
                    setStatus(manual ? 'Refreshed' : `Last updated at ${new Date().toLocaleTimeString()}`);
                } catch (err) {
                    console.error(err);
                    setStatus(`Error updating: ${err.message}`, true);
                }
            }

            refreshButton.addEventListener('click', () => fetchData(true));
            fetchData();
            setInterval(fetchData, 60000);
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)


@app.route("/codes.json")
def codes_json():
    config = _get_config()
    with _state_lock:
        snapshot = {
            "query": config["query"],
            "poll_interval_seconds": config["poll_interval"],
            "max_posts": config["max_posts"],
            "last_poll": _last_poll,
            "candidates": list(reversed(_candidates)),
            "activity_log": list(reversed(_activity_log)),
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
