"""Pull CSVs from a Drive folder and answer questions about them via Claude + a pandas tool."""

import builtins
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
MAX_TOOL_ITERATIONS = 10
TOOL_RESULT_PREVIEW_CHARS = 200
DEFAULT_QUESTION = "What are the top 5 products by sales, and which region drives the most revenue?"
SOURCE_FILE_COLUMN = "__source_file"


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


def _to_jsonable(value: Any) -> Any:
    """Coerce pandas/numpy results to JSON-safe primitives. Caps DataFrames/Series at 50 rows."""
    if isinstance(value, pd.DataFrame):
        return value.head(50).to_dict(orient="records")
    if isinstance(value, pd.Series):
        return value.head(50).to_dict()
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return value


# Restricted globals block the obvious escapes (`__import__`, `open`, `eval`, `exec`)
# but not dunder traversal (`df.__class__.__mro__[...]`). Fine for a personal
# sandbox where the user controls every question; would need RestrictedPython
# or AST validation for multi-tenant use.
_SAFE_BUILTIN_NAMES = (
    "len", "min", "max", "sum", "sorted", "abs", "round",
    "int", "float", "str", "bool", "list", "dict", "tuple", "set",
    "enumerate", "range", "zip", "map", "filter", "any", "all", "type",
)
_SAFE_BUILTINS = {name: getattr(builtins, name) for name in _SAFE_BUILTIN_NAMES}


def run_pandas(df: pd.DataFrame, expr: str) -> Any:
    """Evaluate a pandas expression against `df`. Returns the result or an error dict."""
    try:
        result = eval(expr, {"__builtins__": _SAFE_BUILTINS}, {"df": df, "pd": pd})
        return _to_jsonable(result)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


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
        df[SOURCE_FILE_COLUMN] = drive_file["name"]
        frames.append(df)

    combined = pd.concat(frames, ignore_index=True)
    return combined, files


def _rows_in_file(df: pd.DataFrame, name: str) -> int:
    """Count rows that came from a given source filename."""
    return int((df[SOURCE_FILE_COLUMN] == name).sum())


def _schema_preview(df: pd.DataFrame, files: list[dict]) -> dict:
    """Small structured hint sent to Claude so it can write valid pandas without seeing all rows."""
    return {
        "files": [{"name": f["name"], "rows": _rows_in_file(df, f["name"])} for f in files],
        "total_rows": int(len(df)),
        "columns": [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns],
        "sample": df.head(3).to_dict(orient="records"),
    }


def ask_claude(df: pd.DataFrame, files: list[dict], question: str) -> str:
    """Tool-use loop: Claude reasons → calls run_pandas → reads result → answers in plain prose.

    Using Anthropic's tool-use loop directly (not Composio's provider) — run_pandas
    is a local tool, and the plain loop is the more transferable pattern.
    """
    claude = anthropic.Anthropic()

    tools = [{
        "name": "run_pandas",
        "description": (
            "Evaluate a pandas expression against the loaded DataFrame `df`. "
            "Use this whenever answering requires looking at the data. "
            "Available names: `df` (DataFrame), `pd` (pandas). "
            "DataFrames/Series in the result are truncated to 50 rows. "
            "Examples: "
            "df.groupby('Category')['Sales'].sum().sort_values(ascending=False).to_dict(); "
            "int((df['Region']=='West').sum()); "
            "df[df['State']=='California']['Sales'].sum()."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"expr": {"type": "string", "description": "A pandas expression."}},
            "required": ["expr"],
        },
    }]

    messages: list[dict] = [{
        "role": "user",
        "content": (
            f"Schema and tiny sample of the dataset:\n"
            f"{json.dumps(_schema_preview(df, files), default=str)}\n\n"
            f"Question: {question}\n\n"
            "Use the `run_pandas` tool to query the data when needed. Call it as "
            "many times as you need. When you have enough information, answer the "
            "user in plain prose."
        ),
    }]

    for _ in range(MAX_TOOL_ITERATIONS):
        msg = claude.messages.create(model=MODEL, max_tokens=4096, tools=tools, messages=messages)
        if msg.stop_reason != "tool_use":
            return "".join(b.text for b in msg.content if b.type == "text")

        tool_results = []
        for block in msg.content:
            if block.type == "tool_use" and block.name == "run_pandas":
                expr = block.input["expr"]
                print(f"  → run_pandas: {expr}")
                result = run_pandas(df, expr)
                preview = json.dumps(result, default=str)
                if len(preview) > TOOL_RESULT_PREVIEW_CHARS:
                    preview = preview[:TOOL_RESULT_PREVIEW_CHARS - 3] + "..."
                print(f"    = {preview}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, default=str),
                })

        messages.append({"role": "assistant", "content": msg.content})
        messages.append({"role": "user", "content": tool_results})

    return "[Hit MAX_TOOL_ITERATIONS without a final answer.]"


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

    print(f"Loaded {len(files)} file(s), {len(combined)} total rows:")
    for drive_file in files:
        print(f"  - {drive_file['name']}: {_rows_in_file(combined, drive_file['name'])} rows")
    print()

    question = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUESTION
    print(f"Q: {question}\n")
    print(f"A: {ask_claude(combined, files, question)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
