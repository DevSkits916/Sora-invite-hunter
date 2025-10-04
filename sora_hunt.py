"""Sora Invite Code Hunter web application."""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import requests
from flask import Flask, jsonify, render_template_string

# Configuration defaults
DEFAULT_QUERY = "Sora invite code OR 'Sora 2 invite' OR 'Sora2 invite'"
DEFAULT_USER_AGENT = "sora-hunter/0.1"
DEFAULT_POLL_INTERVAL = 60
DEFAULT_MAX_POSTS = 75

REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
TOKEN_PATTERN = re.compile(r"\b[A-Z0-9]{5,8}\b")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Shared state guarded by a lock
_state_lock = threading.Lock()
_candidates: List[Dict[str, str]] = []
_seen_codes: set[str] = set()
_last_poll: Optional[str] = None


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


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _poll_reddit() -> None:
    """Continuously poll Reddit for invite codes."""
    global _last_poll
    while True:
        start_time = time.time()
        config = _get_config()
        logging.debug("Polling Reddit with interval=%s query=\"%s\" limit=%s", config["poll_interval"], config["query"], config["max_posts"])
        try:
            params = {
                "q": config["query"],
                "sort": "new",
                "limit": config["max_posts"],
                "restrict_sr": False,
            }
            headers = {"User-Agent": config["user_agent"]}
            response = requests.get(REDDIT_SEARCH_URL, params=params, headers=headers, timeout=20)
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data", {}).get("children", [])

            new_candidates: List[Dict[str, str]] = []
            for item in items:
                data = item.get("data", {})
                title = data.get("title", "")
                body = data.get("selftext", "") or ""
                permalink = data.get("permalink") or ""
                url = f"https://www.reddit.com{permalink}" if permalink else data.get("url", "")
                tokens = _extract_tokens(f"{title}\n{body}")

                for token in tokens:
                    with _state_lock:
                        if token in _seen_codes:
                            continue
                    snippet = _build_example_snippet(title, body, token)
                    candidate = {
                        "code": token,
                        "example_text": snippet,
                        "source_title": title,
                        "url": url,
                        "discovered_at": _iso_now(),
                    }
                    with _state_lock:
                        _seen_codes.add(token)
                        _candidates.append(candidate)
                    new_candidates.append(candidate)

            logging.info("Poll completed: %d new candidate(s) found", len(new_candidates))
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("Error while polling Reddit: %s", exc)
        finally:
            with _state_lock:
                _last_poll = _iso_now()

        elapsed = time.time() - start_time
        sleep_for = max(config["poll_interval"] - elapsed, 5)
        time.sleep(sleep_for)


def _start_background_thread() -> None:
    thread = threading.Thread(target=_poll_reddit, name="reddit-poller", daemon=True)
    thread.start()
    logging.info("Background Reddit polling thread started")


@app.route("/")
def index() -> str:
    with _state_lock:
        recent = list(reversed(_candidates))
        last_poll = _last_poll
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8" />
        <title>Sora Invite Code Hunter</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 2rem; background-color: #f5f5f5; }
            h1 { color: #333; }
            table { border-collapse: collapse; width: 100%; background: #fff; }
            th, td { border: 1px solid #ccc; padding: 0.5rem; text-align: left; }
            th { background: #eee; }
            tbody tr:nth-child(odd) { background: #fafafa; }
            code { font-size: 1.2rem; font-weight: bold; }
            .timestamp { white-space: nowrap; }
        </style>
    </head>
    <body>
        <h1>Sora Invite Code Hunter</h1>
        <p>Tracking the latest potential invite codes shared on Reddit. Page auto-refreshes every 60 seconds.</p>
        <p><strong>Last Poll:</strong> {{ last_poll or "not yet" }}</p>
        <table>
            <thead>
                <tr>
                    <th>Code</th>
                    <th>Example Text</th>
                    <th>Source Title</th>
                    <th>Discovered At</th>
                </tr>
            </thead>
            <tbody>
                {% if candidates %}
                    {% for item in candidates %}
                        <tr>
                            <td><a href="{{ item.url }}" target="_blank" rel="noopener"><code>{{ item.code }}</code></a></td>
                            <td>{{ item.example_text|safe }}</td>
                            <td>{{ item.source_title }}</td>
                            <td class="timestamp">{{ item.discovered_at }}</td>
                        </tr>
                    {% endfor %}
                {% else %}
                    <tr><td colspan="4">No candidates found yet. Check back soon!</td></tr>
                {% endif %}
            </tbody>
        </table>
        <script>
            setTimeout(function() { window.location.reload(); }, 60000);
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template, candidates=recent, last_poll=last_poll)


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
