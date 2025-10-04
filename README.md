# Sora Invite Code Hunter

A tiny Flask app that polls Reddit's public search JSON for posts mentioning Sora invite codes, extracts likely codes with a simple regex, and serves a live list at `/` and `/codes.json`.

> Codes are ephemeral. This tool only **discovers** candidates. You still have to try them. Don’t resell codes.

---

## Repo Structure
```
.
├─ README.md
├─ sora_hunt.py
├─ requirements.txt
├─ .replit
├─ replit.nix            # optional for newer Replit stacks
├─ .gitignore
└─ LICENSE
```

---

## Quick Start (Local)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python sora_hunt.py
# open http://127.0.0.1:3000
```

## Quick Start (Replit)
1. Create a new Python Repl.
2. Add all files from this repo.
3. Replit will install from `requirements.txt` automatically.
4. Hit Run. Replit will show a public URL at port 3000.

## Configuration
Environment variables are optional. Defaults are safe.

- `POLL_INTERVAL_SECONDS` poll cadence (default 60)
- `MAX_POSTS` number of Reddit results to scan (default 75)
- `QUERY` search query string (default targets common phrases)
- `USER_AGENT` polite user agent string

You can edit these in `sora_hunt.py` or set environment vars.

## Ethics & Terms
This scrapes Reddit's public JSON endpoint. Be gentle with polling. Don’t buy or resell codes.

---

## Files

### README.md
```markdown
# Sora Invite Code Hunter

Lightweight Flask app that polls Reddit search JSON for posts with possible Sora invite codes. Shows candidates at `/` and `/codes.json`.

## Run locally
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python sora_hunt.py
# http://127.0.0.1:3000
```

## Deploy on Replit
- Create a Python Repl, add these files, press Run.
- Replit reads `.replit` and installs `requirements.txt`.

## Config
Set environment variables or edit constants in the file:
- `POLL_INTERVAL_SECONDS` (default 60)
- `MAX_POSTS` (default 75)
- `QUERY`
- `USER_AGENT`

## Notes
- This discovers **candidate** codes; many will be dead by the time you try them.
- Use responsibly; respect rate limits.
```

### sora_hunt.py
```python
import os
import re
import time
import threading
import requests
from datetime import datetime
from flask import Flask, jsonify, render_template_string

# ===== CONFIG (env overrides) =====
USER_AGENT = os.getenv("USER_AGENT", "sora-hunter/0.1 by travis")
REDDIT_SEARCH_URL = "https://www.reddit.com/search.json"
QUERY = os.getenv("QUERY", "Sora invite code OR \"Sora 2 invite\" OR \"Sora2 invite\"")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "75"))
CODE_RE = re.compile(r"[A-Z0-9]{5,8}")  # short mixed alnum

_seen = set()
_lock = threading.Lock()
_latest = {
    "last_poll": None,
    "candidates": [],  # list of {code, example_text, source_title, url, discovered_at}
}

app = Flask(__name__)


def fetch_reddit(query: str = QUERY, limit: int = MAX_POSTS):
    headers = {"User-Agent": USER_AGENT}
    params = {"q": query, "sort": "new", "limit": limit, "restrict_sr": False}
    r = requests.get(REDDIT_SEARCH_URL, headers=headers, params=params, timeout=12)
    r.raise_for_status()
    return r.json()


def extract_codes_from_text(text: str):
    text = (text or "").upper()
    found = set(m for m in CODE_RE.findall(text) if any(ch.isdigit() for ch in m))
    filtered = set()
    for c in found:
        if len(c) < 5:
            continue
        if c.isalpha():
            continue
        filtered.add(c)
    return filtered


def parse_reddit_for_codes(json_payload):
    out = []
    for child in json_payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        title = d.get("title", "")
        selftext = d.get("selftext", "")
        url = d.get("url", "")
        candidate_text = f"{title}
{selftext}".upper()
        codes = extract_codes_from_text(candidate_text)
        for c in codes:
            out.append({
                "code": c,
                "example_text": title[:220] if title else selftext[:220],
                "source_title": title,
                "url": url,
            })
    return out


def poll_loop():
    global _latest
    while True:
        try:
            payload = fetch_reddit()
            items = parse_reddit_for_codes(payload)
            new_items = []
            now = datetime.utcnow().isoformat() + "Z"
            with _lock:
                for it in items:
                    code = it["code"]
                    if code in _seen:
                        continue
                    _seen.add(code)
                    entry = {
                        "code": code,
                        "example_text": it.get("example_text"),
                        "source_title": it.get("source_title"),
                        "url": it.get("url"),
                        "discovered_at": now,
                    }
                    new_items.append(entry)
                if new_items:
                    _latest["candidates"] = new_items + _latest.get("candidates", [])
                _latest["last_poll"] = now
        except Exception as e:
            with _lock:
                _latest["last_poll"] = f"error: {str(e)}"
        time.sleep(POLL_INTERVAL_SECONDS)


@app.route("/")
def index():
    with _lock:
        last = _latest.get("last_poll")
        candidates = _latest.get("candidates", [])[:200]
    template = """
    <html>
    <head><title>Sora Hunt - live</title></head>
    <body>
        <h2>Sora Invite Code Hunter</h2>
        <p>Last poll: {{last}}</p>
        <p>Note: codes are ephemeral. If one works for you, please don't resell it.</p>
        <h3>Candidates (newest first)</h3>
        <table border=1 cellpadding=6 cellspacing=0>
        <tr><th>Code</th><th>Discovered</th><th>Source</th><th>Snippet</th></tr>
        {% for c in candidates %}
        <tr>
            <td><b>{{c.code}}</b></td>
            <td>{{c.discovered_at}}</td>
            <td><a href="{{c.url}}" target="_blank">link</a></td>
            <td>{{c.example_text}}</td>
        </tr>
        {% endfor %}
        </table>
    </body>
    </html>
    """
    return render_template_string(template, last=last, candidates=candidates)


@app.route("/codes.json")
def codes_json():
    with _lock:
        return jsonify(_latest)


if __name__ == "__main__":
    t = threading.Thread(target=poll_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=3000)
```

### requirements.txt
```text
Flask>=3.0.0
requests>=2.31.0
```

### .replit
```toml
run = "python sora_hunt.py"
```

### replit.nix
```nix
{ pkgs }: {
  deps = [
    pkgs.python311
    pkgs.python311Packages.pip
  ];
}
```

### .gitignore
```gitignore
# virtual envs
.venv/
venv/
# py cache
__pycache__/
*.pyc
# Replit
.repl*
```

### LICENSE
```text
MIT License

Copyright (c) 2025 Travis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
