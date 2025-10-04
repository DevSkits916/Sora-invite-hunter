You want a crisp spec and a turnkey README so a code-gen model can crank out the repo without getting lost. Fine. Here’s both: an implementation outline (clear enough for an LLM to map to code) and a clean `README.md` you can paste straight into GitHub. The artifacts below are written neutrally on purpose so the robot army doesn’t hallucinate your sarcasm into function names.

---

# Implementation Outline (Functional + Technical)

## 1) Problem & Goal

* **Problem:** Sora invite codes are posted in chaotic public threads and expire fast. Manual hunting wastes time.
* **Goal:** A lightweight app that continuously scans public sources (starting with Reddit search JSON), extracts likely codes via regex, de-duplicates them, and serves a live list via a tiny web UI and JSON endpoint. Easy to deploy on Replit or locally.

## 2) Core Features (MVP)

1. **Polling worker**

   * Periodically query Reddit’s public search JSON API for configured terms.
   * Parse titles/selftext for short alphanumeric tokens that match code-like patterns.
   * Heuristics to reduce false positives (must include at least one digit, length 5–8, skip all-alpha tiny tokens, maintain denylist).
   * Maintain an in-memory set to avoid re-emitting duplicates.

2. **Storage**

   * In-memory list of candidate codes with timestamp and source URL.
   * Optional: pluggable persistence layer (JSON file or SQLite) via env flag.

3. **Web API + UI**

   * `GET /` simple HTML table showing candidates, newest first.
   * `GET /codes.json` returns current snapshot: last poll time, candidates array.
   * CORS-safe, no auth required by default.

4. **Configuration**

   * Environment variables for poll interval, query, max posts, user agent, source toggles.
   * Sensible defaults so it runs out of the box.

5. **Ethics/Rate Limits**

   * Polite polling interval, user agent string, no abusive scraping.

## 3) Nice-to-Have (Post-MVP)

* Multiple sources: X/Twitter (requires API or puppeteer-like headless), Discord channels (if allowed), specialized forums.
* Persistence: enable `STORE=sqlite` with schema migration.
* Web UI extras: copy button, “mark tried,” export CSV.
* Webhook to Discord or Slack when new code appears.
* Simple keyword filters or per-subreddit targeting.
* Basic auth gate for `/` if you don’t want it public.

## 4) Inputs & Outputs

* **Input:** Public JSON search responses (Reddit at launch).
* **Processing:** Regex identify `[A-Z0-9]{5,8}` tokens; filter; de-dupe; enrich with source link.
* **Output:** HTML table at `/` and JSON feed at `/codes.json`.

## 5) Data Model

```ts
type CandidateCode = {
  code: string;            // e.g. "7ZDCNP"
  example_text: string;    // short snippet from title/selftext
  source_title: string;    // original post title
  url: string;             // source post URL
  discovered_at: string;   // ISO timestamp (UTC)
};

type Snapshot = {
  last_poll: string;       // ISO timestamp or "error: ..."
  candidates: CandidateCode[];
};
```

## 6) API Surface

* `GET /`
  Renders HTML table of codes. No query params.
* `GET /codes.json`
  Returns `Snapshot` JSON object.
* Health check (optional): `GET /healthz` returns `{ ok: true }`.

## 7) Configuration (Env Vars)

* `POLL_INTERVAL_SECONDS` default `60`
* `MAX_POSTS` default `75`
* `QUERY` default `Sora invite code OR "Sora 2 invite" OR "Sora2 invite"`
* `USER_AGENT` default `sora-hunter/0.1`
* `STORE` optional: `memory` (default) or `json` or `sqlite`
* `STORE_PATH` when `STORE=json`, e.g. `data/codes.json`

## 8) Architecture

* **Process layout:** single process, one background polling thread, one Flask app thread.
* **Concurrency:** thread-safe updates guarded by a lock.
* **State:** memory or optional persistence file/SQLite.

## 9) File/Repo Structure

```
.
├─ README.md
├─ sora_hunt.py               # Flask app + polling worker
├─ requirements.txt
├─ .replit                    # Replit run config
├─ replit.nix                 # optional Replit nix env
├─ .gitignore
└─ LICENSE
```

## 10) Pseudocode

```python
global_seen = set()
latest = { "last_poll": None, "candidates": [] }
lock = threading.Lock()

def poll_loop():
    while True:
        try:
            payload = fetch_reddit_json(query=QUERY, limit=MAX_POSTS)
            items = parse_items(payload)  # yield (title, selftext, url)
            now = utc_now_iso()
            new_batch = []
            for title, selftext, url in items:
                codes = extract_codes(title + "\n" + selftext)
                for c in codes:
                    if c not in global_seen:
                        global_seen.add(c)
                        new_batch.append({
                            "code": c,
                            "example_text": title[:220] or selftext[:220],
                            "source_title": title,
                            "url": url,
                            "discovered_at": now
                        })
            with lock:
                latest["last_poll"] = now
                if new_batch:
                    latest["candidates"] = new_batch + latest["candidates"]
                persist_if_enabled(latest)
        except Exception as e:
            with lock:
                latest["last_poll"] = f"error: {e}"
        sleep(POLL_INTERVAL_SECONDS)

# Flask routes read from 'latest' under lock and render HTML/JSON.
```

## 11) Validation & Testing

* Unit tests for `extract_codes_from_text` with false-positive words and edge cases.
* Smoke test: app starts, `/` and `/codes.json` return 200.
* Rate limit check: ensure interval respected.
* Regression: repeated polling doesn’t duplicate entries.

## 12) Constraints & Risks

* Public endpoints can change or throttle; keep intervals modest.
* Many extracted tokens will be dead. This is a discovery tool, not a guarantee machine.
* Avoid reselling codes; follow platform terms.

---

# `README.md` (Paste into GitHub)

````markdown
# Sora Invite Code Hunter

Lightweight Flask app that polls public Reddit search results for posts that might contain **Sora invite codes**, extracts likely codes with simple heuristics, and serves a live feed at a web page (`/`) and JSON endpoint (`/codes.json`).

> **Note:** This discovers **candidate** codes only. Codes are ephemeral and may be invalid by the time you try them. Use responsibly. Do not buy or resell codes.

---

## Features
- Background poller scans Reddit search JSON at a configurable interval
- Regex extraction for short mixed alphanumeric tokens (length 5–8) with heuristic filters
- De-duplication and timestamping
- Minimal web UI at `/` and machine-readable JSON at `/codes.json`
- One-file deploy; runs on Replit or locally

## Quick Start (Local)
```bash
git clone https://github.com/<your-username>/sora-invite-hunt.git
cd sora-invite-hunt
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python sora_hunt.py
# open http://127.0.0.1:3000
````

## Quick Start (Replit)

1. Create a **Python** Repl.
2. Add the repo files.
3. Replit installs `requirements.txt` automatically.
4. Press **Run** and open the public URL (port `3000`).

## Configuration

All config is optional and can be set via environment variables:

| Variable                | Default                                                 | Description                                      |
| ----------------------- | ------------------------------------------------------- | ------------------------------------------------ |
| `POLL_INTERVAL_SECONDS` | `60`                                                    | Seconds between polls                            |
| `MAX_POSTS`             | `75`                                                    | Number of search results to scan                 |
| `QUERY`                 | `Sora invite code OR "Sora 2 invite" OR "Sora2 invite"` | Search query                                     |
| `USER_AGENT`            | `sora-hunter/0.1`                                       | HTTP user-agent string                           |
| `STORE`                 | `memory`                                                | `memory`, `json`, or `sqlite` (optional feature) |
| `STORE_PATH`            | `data/codes.json`                                       | Path used when `STORE=json`                      |

Set via shell:

```bash
export POLL_INTERVAL_SECONDS=90
export MAX_POSTS=100
export QUERY='Sora invite code'
python sora_hunt.py
```

## How It Works

1. A background thread fetches Reddit search JSON with the configured query.
2. The app extracts code-like tokens with a regex and filters obvious false positives.
3. New candidates are de-duplicated and prepended to the in-memory list.
4. The Flask app exposes:

   * **`GET /`**: simple HTML table of candidates (newest first)
   * **`GET /codes.json`**: JSON snapshot `{ last_poll, candidates[] }`

### Data Model

```json
{
  "last_poll": "2025-10-04T15:25:33Z",
  "candidates": [
    {
      "code": "7ZDCNP",
      "example_text": "Thread title or short snippet...",
      "source_title": "Original post title",
      "url": "https://www.reddit.com/...",
      "discovered_at": "2025-10-04T15:25:33Z"
    }
  ]
}
```

## File Structure

```
.
├─ README.md
├─ sora_hunt.py
├─ requirements.txt
├─ .replit
├─ replit.nix
├─ .gitignore
└─ LICENSE
```

## Endpoints

* `GET /` — HTML table with candidates
* `GET /codes.json` — JSON snapshot
* Optional: `GET /healthz` — returns `{ "ok": true }` (add if needed)

## Limitations

* Public endpoints change or throttle; keep intervals reasonable.
* Regex is heuristic and will include some false positives by design.
* This is for discovery only; many codes expire quickly.
* Do not use this to buy/sell invites or violate platform terms.

## Roadmap (Optional)

* Additional sources (X/Twitter API, Discord channels where allowed)
* SQLite persistence and basic search
* Webhook notifications (Discord/Slack)
* Mark-as-tried UI and CSV export
* Per-subreddit or keyword filters

## Development

Run tests (if you add them):

```bash
pytest -q
```

Lint/format (if enabled):

```bash
ruff check .
black .
```

## License

MIT

```

---

# Extra: “Codex Build Notes” (for codegen prompts)

Use this when you feed the outline to a code model:

- Language: Python 3.11+
- Web framework: Flask
- Dependencies: `Flask>=3.0.0`, `requests>=2.31.0`
- Single-file app with background thread for polling
- Expose `GET /` and `GET /codes.json`
- Configuration via environment variables listed above
- Include basic HTML template string for `/`
- Implement graceful error reporting by setting `latest["last_poll"] = "error: <msg>"`
- Regex: `\b[A-Z0-9]{5,8}\b`, require at least one digit
- De-duplication: global `set()` of seen codes
- Thread-safety: `threading.Lock()` around shared state
- Default port 3000, bind `0.0.0.0`
- Optional persistence behind env switch is OK but not required for MVP

There. Now you’ve got a blueprint and a README that won’t embarrass you in public. Go feed it to your favorite code genie and pretend it was easy.
::contentReference[oaicite:0]{index=0}
```
