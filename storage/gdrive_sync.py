# ============================================================
# storage/gdrive_sync.py
# Syncs local Parquet data files to Google Drive.
#
# SETUP (one-time):
#   1. Go to Google Cloud Console → APIs & Services → Enable "Google Drive API"
#   2. Create a Service Account → Download JSON key
#   3. Save the JSON key as config/gdrive_credentials.json
#   4. Share your Google Drive folder with the service account email
#
# The sync is incremental — only uploads files that are newer
# than what's already on Drive.
# ============================================================

import logging
import json
from pathlib import Path
from datetime import datetime
import sys

sys.path.append(str(Path(__file__).parent.parent))
from config.settings import DATA_DIR, GDRIVE_FOLDER_NAME, GDRIVE_CREDENTIALS_FILE

logger = logging.getLogger(__name__)


def get_drive_service():
    """Authenticate and return a Google Drive API service object."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        raise ImportError(
            "Google Drive libraries not installed.\n"
            "Run: pip install google-api-python-client google-auth"
        )

    if not GDRIVE_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Google credentials not found at {GDRIVE_CREDENTIALS_FILE}\n"
            "See setup instructions in this file's docstring."
        )

    creds = service_account.Credentials.from_service_account_file(
        str(GDRIVE_CREDENTIALS_FILE),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, name: str, parent_id: str = None) -> str:
    """Get a Drive folder by name (creates it if it doesn't exist)."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])

    if files:
        return files[0]["id"]

    # Create the folder
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        meta["parents"] = [parent_id]

    folder = service.files().create(body=meta, fields="id").execute()
    logger.info(f"Created Drive folder: {name} (id={folder['id']})")
    return folder["id"]


def file_exists_on_drive(service, name: str, parent_id: str) -> str | None:
    """Return the file ID if it exists on Drive, else None."""
    query = f"name='{name}' and '{parent_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields="files(id, modifiedTime)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def upload_file(service, local_path: Path, parent_id: str):
    """Upload or update a single file on Google Drive."""
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local_path), mimetype="application/octet-stream", resumable=True)
    file_name = local_path.name

    existing_id = file_exists_on_drive(service, file_name, parent_id)

    if existing_id:
        # Update existing file
        service.files().update(
            fileId=existing_id,
            media_body=media,
        ).execute()
        logger.debug(f"Updated: {local_path.name}")
    else:
        # Upload new file
        meta = {"name": file_name, "parents": [parent_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
        logger.debug(f"Uploaded: {local_path.name}")


def sync_to_drive(changed_symbols: list[str] = None):
    """
    Sync local data to Google Drive.
    If changed_symbols is given, only those are synced.
    Otherwise all Parquet files are synced.

    Drive structure mirrors local:
      NiftyFNO_MarketData/
        NSE_RELIANCE_EQ/
          2023/
            ohlcv_5min.parquet
          2024/
            ohlcv_5min.parquet
        NSE_INFY_EQ/
          ...
    """
    logger.info("Starting Google Drive sync...")
    service = get_drive_service()

    # Get or create root folder on Drive
    root_id = get_or_create_folder(service, GDRIVE_FOLDER_NAME)

    # Find all parquet files to sync
    if changed_symbols:
        parquet_files = []
        for sym in changed_symbols:
            clean = sym.replace(":", "_").replace("-", "_")
            parquet_files.extend((DATA_DIR / clean).rglob("ohlcv_5min.parquet"))
    else:
        parquet_files = list(DATA_DIR.rglob("ohlcv_5min.parquet"))

    total = len(parquet_files)
    logger.info(f"Syncing {total} Parquet files to Drive...")

    for i, local_path in enumerate(parquet_files, 1):
        # local_path = data/NSE_RELIANCE_EQ/2023/ohlcv_5min.parquet
        # Drive path  = NiftyFNO_MarketData/NSE_RELIANCE_EQ/2023/ohlcv_5min.parquet
        parts = local_path.relative_to(DATA_DIR).parts   # ('NSE_RELIANCE_EQ', '2023', 'ohlcv_5min.parquet')

        # Ensure parent folders exist on Drive
        current_parent = root_id
        for folder_name in parts[:-1]:
            current_parent = get_or_create_folder(service, folder_name, current_parent)

        upload_file(service, local_path, current_parent)

        if i % 20 == 0:
            logger.info(f"Drive sync progress: {i}/{total}")

    logger.info(f"Drive sync complete. {total} files synced.")
