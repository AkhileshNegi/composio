# Composio + Claude over Google Drive CSVs

Minimal sandbox: Claude answers questions about CSV files in a Google Drive
folder. Drop a new CSV in the folder, re-run the agent, and the answers
reflect the new data — no code changes.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the 3 values
```

`.env` needs:
- `COMPOSIO_API_KEY` — https://app.composio.dev
- `ANTHROPIC_API_KEY` — https://console.anthropic.com
- `DRIVE_FOLDER_ID` — the segment after `/folders/` in the Drive folder URL

## Run

```bash
python connect.py       # one-time: opens Google OAuth, links Drive to Composio
python agent.py         # each run: pulls CSVs, summarizes, asks Claude
```

## Adding more data

Upload another CSV to the **same** Drive folder, then re-run `python agent.py`.
The agent lists the folder fresh each run, so the new file is picked up
automatically and merged into the dataset Claude sees.
