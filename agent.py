"""Pull CSVs from a Drive folder, summarize, ask Claude a question."""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

import anthropic
import pandas as pd
from composio import Composio
from dotenv import load_dotenv

USER_ID = "local-dev"
MODEL = "claude-opus-4-7"
CACHE_DIR = Path("downloaded_csvs")
CSV_MIME = "text/csv"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


class NoDataError(RuntimeError):
    """Drive folder has no CSV or Sheet files we can read."""


def normalize_folder_id(value: str) -> str:
    """Accept either a bare folder ID or a full Drive URL."""
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    return match.group(1) if match else value.strip()


def _payload(resp: Any) -> dict:
    """Extract the `data` envelope from a Composio tool response, regardless of object/dict shape."""
    if hasattr(resp, "model_dump"):
        resp = resp.model_dump()
    elif hasattr(resp, "__dict__") and not isinstance(resp, dict):
        resp = dict(resp.__dict__)
    if not isinstance(resp, dict):
        return {}
    inner = resp.get("data", resp)
    return inner if isinstance(inner, dict) else {}


def _staged_url(payload: dict) -> str | None:
    """Composio stages downloaded/exported files in R2 and returns a presigned URL under one of these keys."""
    for key in ("downloaded_file_content", "exported_file_content", "file"):
        node = payload.get(key)
        if isinstance(node, dict) and node.get("s3url"):
            return node["s3url"]
    val = payload.get("path") or payload.get("file")
    return val if isinstance(val, str) else None


def list_csvs(composio: Composio, folder_id: str) -> list[dict]:
    """List CSVs and Google Sheets in the folder (Sheets get exported as CSV later)."""
    q = (
        f"'{folder_id}' in parents and trashed = false "
        f"and (mimeType = '{CSV_MIME}' or mimeType = '{SHEET_MIME}')"
    )
    resp = composio.tools.execute(
        "GOOGLEDRIVE_FIND_FILE",
        user_id=USER_ID,
        arguments={"q": q},
        dangerously_skip_version_check=True,
    )
    return _payload(resp).get("files") or []


def fetch_as_csv(composio: Composio, drive_file: dict) -> Path:
    """Download a CSV directly, or export a Sheet as CSV via Composio, into CACHE_DIR."""
    file_id = drive_file["id"]
    name = drive_file["name"]
    mime = drive_file.get("mimeType", "")

    if mime == SHEET_MIME:
        resp = composio.tools.execute(
            "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
            user_id=USER_ID,
            arguments={"fileId": file_id, "mimeType": CSV_MIME},
            dangerously_skip_version_check=True,
        )
        dest_name = name if name.lower().endswith(".csv") else f"{name}.csv"
    else:
        resp = composio.tools.execute(
            "GOOGLEDRIVE_DOWNLOAD_FILE",
            user_id=USER_ID,
            arguments={"file_id": file_id},
            dangerously_skip_version_check=True,
        )
        dest_name = name

    payload = _payload(resp)
    url = _staged_url(payload)
    if not url:
        raise RuntimeError(f"Could not locate downloaded file in response: {payload!r}")

    dest = CACHE_DIR / dest_name
    with urllib.request.urlopen(url) as response:
        dest.write_bytes(response.read())
    return dest


def load_dataset(composio: Composio, folder_id: str) -> tuple[pd.DataFrame, list[dict]]:
    """List, fetch, and concatenate every CSV/Sheet in the folder. Raises NoDataError if empty."""
    CACHE_DIR.mkdir(exist_ok=True)
    files = list_csvs(composio, folder_id)
    if not files:
        raise NoDataError(f"No CSV or Sheet files found in folder {folder_id}.")

    frames = []
    for drive_file in files:
        local = fetch_as_csv(composio, drive_file)
        df = pd.read_csv(local, encoding_errors="replace")
        df["__source_file"] = drive_file["name"]
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    return combined, files


def build_summary(combined: pd.DataFrame, files: list[dict]) -> dict:
    per_file = [
        {"name": f["name"], "rows": int((combined["__source_file"] == f["name"]).sum())}
        for f in files
    ]
    return {
        "files": per_file,
        "total_rows": int(len(combined)),
        "columns": [{"name": c, "dtype": str(combined[c].dtype)} for c in combined.columns],
        "describe": json.loads(combined.describe(include="all").to_json(default_handler=str)),
        "sample": combined.head(10).to_dict(orient="records"),
    }


def ask_claude(summary: dict, question: str) -> str:
    claude = anthropic.Anthropic()
    msg = claude.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": (
                f"You are answering questions about a sales dataset assembled from "
                f"{len(summary['files'])} CSV file(s) in a Google Drive folder.\n\n"
                f"Dataset summary (JSON):\n{json.dumps(summary, default=str)}\n\n"
                f"Question: {question}\n\n"
                "Answer using only the summary above. If the summary doesn't contain "
                "enough detail to answer precisely, say so and describe what you'd need."
            ),
        }],
    )
    return "".join(block.text for block in msg.content if block.type == "text")


DEFAULT_QUESTION = "What are the top 5 products by sales, and which region drives the most revenue?"


def main() -> int:
    load_dotenv()
    raw_folder = os.environ.get("DRIVE_FOLDER_ID")
    if not raw_folder:
        print("DRIVE_FOLDER_ID is not set in .env", file=sys.stderr)
        return 1
    folder_id = normalize_folder_id(raw_folder)

    composio = Composio()
    try:
        combined, files = load_dataset(composio, folder_id)
    except NoDataError as exc:
        print(exc, file=sys.stderr)
        return 1

    summary = build_summary(combined, files)
    print(f"Loaded {len(files)} file(s), {len(combined)} total rows:")
    for entry in summary["files"]:
        print(f"  - {entry['name']}: {entry['rows']} rows")
    print()

    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    print(f"Q: {question}\n")
    print("A:", ask_claude(summary, question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
