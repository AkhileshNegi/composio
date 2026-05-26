"""One-time: connect Google Drive to Composio for USER_ID via OAuth."""

import sys
from dotenv import load_dotenv
from composio import Composio

USER_ID = "local-dev"
TOOLKIT = "GOOGLEDRIVE"


def main() -> int:
    load_dotenv()
    composio = Composio()

    existing = composio.client.connected_accounts.list(
        user_ids=[USER_ID],
        toolkit_slugs=[TOOLKIT],
        statuses=["ACTIVE"],
    )
    items = getattr(existing, "items", None) or []
    if items:
        acct = items[0]
        acct_id = getattr(acct, "id", None) or (acct.get("id") if isinstance(acct, dict) else None)
        print(f"Already connected: {acct_id}")
        return 0

    auth_configs = composio.auth_configs.list(toolkit_slug=TOOLKIT)
    ac_items = getattr(auth_configs, "items", None) or []
    if not ac_items:
        print(
            f"No auth config found for {TOOLKIT} in this Composio project.\n\n"
            f"Enable it once in the dashboard:\n"
            f"  1. Open https://app.composio.dev/apps\n"
            f"  2. Search for 'Google Drive' and click 'Setup integration'\n"
            f"  3. Choose 'Use Composio's OAuth app' (no Google Cloud setup needed)\n"
            f"  4. Click 'Create integration' — this creates an auth config\n"
            f"  5. Re-run: python connect.py",
            file=sys.stderr,
        )
        return 1
    # Prefer Composio-managed if multiple exist (avoids picking a custom OAuth app accidentally).
    managed = next(
        (
            ac for ac in ac_items
            if (getattr(ac, "is_composio_managed", None)
                or (isinstance(ac, dict) and ac.get("is_composio_managed")))
        ),
        ac_items[0],
    )
    auth_config_id = getattr(managed, "id", None) or managed["id"]
    print(f"Using auth config: {auth_config_id}")

    request = composio.connected_accounts.link(user_id=USER_ID, auth_config_id=auth_config_id)
    print(f"Open this URL in your browser to authorize Google Drive:\n\n  {request.redirect_url}\n")
    print("Waiting for the OAuth callback…")

    connected = request.wait_for_connection()
    conn_id = getattr(connected, "id", None) or connected.get("id")
    print(f"Connected: {conn_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
