"""Pull CSVs from a Drive folder, summarize, ask Claude a question."""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import anthropic
import pandas as pd
from composio import Composio
from dotenv import load_dotenv

USER_ID = "local-dev"
MODEL = "claude-opus-4-7"
CACHE_DIR = Path("downloaded_csvs")


def _unwrap(resp):
    """Composio responses are sometimes objects, sometimes dicts — normalize."""
    if hasattr(resp, "model_dump"):
        return resp.model_dump()
    if hasattr(resp, "__dict__") and not isinstance(resp, dict):
        return {**resp.__dict__}
    return resp


def normalize_folder_id(value: str) -> str:
    """Accept either a bare folder ID or a full Drive URL."""
    m = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
    return m.group(1) if m else value.strip()


CSV_MIME = "text/csv"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def list_csvs(composio: Composio, folder_id: str) -> list[dict]:
    """List CSVs and Google Sheets in the folder (Sheets exported as CSV later)."""
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
    data = _unwrap(resp)
    payload = data.get("data", data) if isinstance(data, dict) else {}
    return payload.get("files") or []


def _staged_url(payload: dict) -> str | None:
    # Download tool nests under `downloaded_file_content`; export tool uses `file`.
    for key in ("downloaded_file_content", "exported_file_content", "file"):
        node = payload.get(key)
        if isinstance(node, dict) and node.get("s3url"):
            return node["s3url"]
    val = payload.get("path") or payload.get("file")
    return val if isinstance(val, str) else None


def download_to_local(composio: Composio, f: dict) -> Path:
    """Download a CSV directly, or export a Sheet as CSV via Composio."""
    file_id, name, mime = f["id"], f["name"], f.get("mimeType", "")

    if mime == SHEET_MIME:
        resp = composio.tools.execute(
            "GOOGLEDRIVE_EXPORT_GOOGLE_WORKSPACE_FILE",
            user_id=USER_ID,
            arguments={"fileId": file_id, "mimeType": CSV_MIME},
            dangerously_skip_version_check=True,
        )
        dest_name = f"{name}.csv" if not name.lower().endswith(".csv") else name
    else:
        resp = composio.tools.execute(
            "GOOGLEDRIVE_DOWNLOAD_FILE",
            user_id=USER_ID,
            arguments={"file_id": file_id},
            dangerously_skip_version_check=True,
        )
        dest_name = name

    data = _unwrap(resp)
    payload = data.get("data", data) if isinstance(data, dict) else {}
    url = _staged_url(payload)
    if not url:
        raise RuntimeError(f"Could not locate downloaded file in response: {data!r}")

    dest = CACHE_DIR / dest_name
    with urllib.request.urlopen(url) as r:
        dest.write_bytes(r.read())
    return dest


def load_dataset(composio: Composio, folder_id: str) -> tuple[pd.DataFrame, list[dict]]:
    CACHE_DIR.mkdir(exist_ok=True)
    files = list_csvs(composio, folder_id)
    if not files:
        raise SystemExit(f"No CSV files found in folder {folder_id}.")

    frames = []
    for f in files:
        local = download_to_local(composio, f)
        df = pd.read_csv(local, encoding_errors="replace")
        df["__source_file"] = f["name"]
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


def main() -> int:
    load_dotenv()
    folder_id = os.environ.get("DRIVE_FOLDER_ID")
    if not folder_id:
        print("DRIVE_FOLDER_ID is not set in .env", file=sys.stderr)
        return 1

    composio = Composio()
    combined, files = load_dataset(composio, folder_id)

    summary = build_summary(combined, files)
    print(f"Loaded {len(files)} file(s), {len(combined)} total rows:")
    for f in summary["files"]:
        print(f"  - {f['name']}: {f['rows']} rows")
    print()

    question = (
        sys.argv[1] if len(sys.argv) > 1
        else "What are the top 5 products by sales, and which region drives the most revenue?"
    )
    print(f"Q: {question}\n")
    print("A:", ask_claude(summary, question))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
