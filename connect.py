"""One-time: connect Google Drive to Composio for USER_ID via OAuth."""

import sys
from typing import Any

from composio import Composio
from dotenv import load_dotenv

USER_ID = "local-dev"
TOOLKIT = "GOOGLEDRIVE"

SETUP_HINT = (
    f"No auth config found for {TOOLKIT} in this Composio project.\n\n"
    "Enable it once in the dashboard:\n"
    "  1. Open https://app.composio.dev/apps\n"
    "  2. Search for 'Google Drive' and click 'Setup integration'\n"
    "  3. Choose 'Use Composio's OAuth app' (no Google Cloud setup needed)\n"
    "  4. Click 'Create integration' — this creates an auth config\n"
    "  5. Re-run: python connect.py"
)


def _attr(obj: Any, name: str) -> Any:
    """Read `name` from an SDK object or a dict — Composio responses come back in either shape."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def main() -> int:
    load_dotenv()
    composio = Composio()

    existing = composio.client.connected_accounts.list(
        user_ids=[USER_ID],
        toolkit_slugs=[TOOLKIT],
        statuses=["ACTIVE"],
    )
    items = _attr(existing, "items") or []
    if items:
        print(f"Already connected: {_attr(items[0], 'id')}")
        return 0

    auth_configs = composio.auth_configs.list(toolkit_slug=TOOLKIT)
    ac_items = _attr(auth_configs, "items") or []
    if not ac_items:
        print(SETUP_HINT, file=sys.stderr)
        return 1

    # Prefer Composio-managed if multiple exist (avoids picking a custom OAuth app accidentally).
    managed = next((ac for ac in ac_items if _attr(ac, "is_composio_managed")), ac_items[0])
    auth_config_id = _attr(managed, "id")
    print(f"Using auth config: {auth_config_id}")

    request = composio.connected_accounts.link(user_id=USER_ID, auth_config_id=auth_config_id)
    print(f"Open this URL in your browser to authorize Google Drive:\n\n  {request.redirect_url}\n")
    print("Waiting for the OAuth callback…")

    connected = request.wait_for_connection()
    print(f"Connected: {_attr(connected, 'id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
