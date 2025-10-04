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
_activity_log: List[Dict[str, str]] = []


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


def _poll_reddit() -> None:
    """Continuously poll Reddit for invite codes."""
    global _last_poll
    while True:
        start_time = time.time()
        config = _get_config()
        logging.debug("Polling Reddit with interval=%s query=\"%s\" limit=%s", config["poll_interval"], config["query"], config["max_posts"])
        _log_event(
            f"Polling Reddit (query=\"{config['query']}\", limit={config['max_posts']})",
            "info",
        )
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
                    _log_event(
                        f"New candidate {token} found in '{title or 'unknown source'}'",
                        "success",
                    )

            logging.info("Poll completed: %d new candidate(s) found", len(new_candidates))
            if new_candidates:
                _log_event(
                    f"Discovered {len(new_candidates)} new candidate(s)",
                    "success",
                )
            else:
                _log_event("No new candidates found this cycle", "info")
        except Exception as exc:  # pylint: disable=broad-except
            logging.exception("Error while polling Reddit: %s", exc)
            _log_event(f"Error while polling Reddit: {exc}", "error")
        finally:
            with _state_lock:
                _last_poll = _iso_now()
            _log_event("Polling cycle finished", "debug")

        elapsed = time.time() - start_time
        sleep_for = max(config["poll_interval"] - elapsed, 5)
        time.sleep(sleep_for)


def _start_background_thread() -> None:
    thread = threading.Thread(target=_poll_reddit, name="reddit-poller", daemon=True)
    thread.start()
    logging.info("Background Reddit polling thread started")
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
        <p>Tracking the latest potential invite codes shared on Reddit. Data refreshes automatically every minute or on demand.</p>
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
