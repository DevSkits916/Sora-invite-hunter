# Sora Invite Code Hunter

Sora Invite Code Hunter is a lightweight Flask application that continuously scans Reddit for potential Sora invite codes and presents the latest findings in a friendly dashboard and JSON feed. A background worker polls Reddit's public search API, extracts candidate codes with a strict pattern, and keeps a deduplicated in-memory list for quick monitoring.

## Features

- 🔁 Background polling thread with configurable interval and search query
- 🔎 Regex-based extraction of 5–8 character alpha-numeric codes that contain at least one digit
- 🧠 In-memory deduplication with thread-safe access
- 🌐 Clean HTML dashboard and JSON API for integrations
- 🌍 Polls Reddit searches, targeted subreddits, and proxied X/Twitter live feeds for fresh leads
- ⚙️ Runtime configuration through environment variables

## Quick Start

### Run locally

1. **Create a virtual environment** (Python 3.11+ recommended):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Launch the app**:
   ```bash
   python sora_hunt.py
   ```
4. Visit [http://localhost:3000](http://localhost:3000) to view the dashboard.

### Run on Replit

1. Import this repository into Replit.
2. Replit detects `replit.nix` and prepares the Python 3.11 environment.
3. Click **Run** (configured in `.replit`) to start `python sora_hunt.py`.
4. Open the hosted URL to access the dashboard.

## Configuration

Use environment variables to adjust runtime behavior without changing code:

| Variable | Default | Description |
| --- | --- | --- |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between Reddit polling cycles (minimum enforced: 10 seconds). |
| `MAX_POSTS` | `75` | Maximum number of Reddit search results to inspect per poll (clamped to 1–100). |
| `QUERY` | `Sora invite code OR 'Sora 2 invite' OR 'Sora2 invite'` | Reddit search query string. |
| `USER_AGENT` | `sora-hunter/0.1` | User-Agent header sent to Reddit's API. |
| `PORT` | `3000` | Port the Flask app listens on when run directly. |
| `HOST` | `0.0.0.0` | Bind address when running the Flask development server. |

Set variables inline when launching:

```bash
POLL_INTERVAL_SECONDS=30 QUERY="Sora invite" python sora_hunt.py
```

### Data Sources

Each polling cycle collects potential codes from a mix of sources:

- The configurable Reddit search query (default: `Sora invite code OR 'Sora 2 invite' OR 'Sora2 invite'`).
- A focused Reddit search for "Sora invite code" plus an additional "Sora beta code" query.
- The newest posts from `/r/ChatGPT`, `/r/OpenAI`, and `/r/SoraAI`.
- Live X/Twitter searches for both `Sora invite code` and the `#SoraInvite` hashtag, proxied through [r.jina.ai](https://r.jina.ai/) to retrieve text content without authentication.

Adding an invite code in any of these places will quickly surface on the dashboard once the next poll completes.

## API Endpoints

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Human-friendly HTML table showing the newest candidate codes first. |
| `GET` | `/codes.json` | JSON payload containing configuration snapshot, last poll timestamp, and candidate list. |

## Candidate Data Model

Each candidate entry returned by `/codes.json` looks like this:

```json
{
  "code": "S0RA1",
  "example_text": "... excerpt from the Reddit post ...",
  "source_title": "Post title containing the code",
  "url": "https://www.reddit.com/r/example/comments/abc123/example",
  "discovered_at": "2024-04-10T12:34:56.789123+00:00"
}
```

The list is deduplicated by `code` and ordered newest-first when served.

## License

This project is distributed under the [MIT License](LICENSE).
