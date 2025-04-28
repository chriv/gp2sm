# database_manager.py (v2.0 - Add Quota Status)
# Handles SQLite database operations for gp2sm transfer state.

import sqlite3
import logging
import os
import json # To store complex metadata if needed
import datetime # For timestamp updates

logger = logging.getLogger(__name__)

# --- Constants ---
DB_FILE_DEFAULT = "gp2sm_transfer_state.db"
MEDIA_TABLE_NAME = "media_items"
CONFIG_TABLE_NAME = "run_config" # Table for config snapshot

# --- Status Codes ---
STATUS_PENDING = "PENDING"
STATUS_HASHED = "HASHED" # MD5 calculated (for images)
STATUS_SMUGMUG_CHECKED_NOT_FOUND = "SMUGMUG_CHECKED_NOT_FOUND"
STATUS_DOWNLOADED_FOR_UPLOAD = "DOWNLOADED_FOR_UPLOAD" # File ready in temp dir
STATUS_UPLOAD_ATTEMPTED = "UPLOAD_ATTEMPTED" # Upload POST request sent
STATUS_UPLOADED_SUCCESS = "UPLOADED_SUCCESS"
STATUS_DUPLICATE_HASH = "DUPLICATE_HASH"
STATUS_DUPLICATE_FILENAME = "DUPLICATE_FILENAME"
STATUS_SKIPPED_FILTER = "SKIPPED_FILTER" # e.g., --ignore-photos
STATUS_SKIPPED_HEIC = "SKIPPED_HEIC"
STATUS_ERROR_DOWNLOAD = "ERROR_DOWNLOAD"
STATUS_ERROR_HASHING = "ERROR_HASHING"
STATUS_ERROR_SMUGMUG_API = "ERROR_SMUGMUG_API" # General SM API error during check/upload
STATUS_ERROR_UPLOAD_FAILED = "ERROR_UPLOAD_FAILED" # Upload POST failed
STATUS_ERROR_ALBUM_FULL = "ERROR_ALBUM_FULL" # SmugMug album limit reached
STATUS_ERROR_QUOTA = "ERROR_QUOTA" # Google API Quota Exceeded (NEW)
STATUS_ERROR_UNKNOWN = "ERROR_UNKNOWN"
STATUS_ERROR_MISSING_DATA = "ERROR_MISSING_DATA" # Item lacks essential fields

# List of terminal success/skip statuses (won't be retried by default)
TERMINAL_STATUSES = [
    STATUS_UPLOADED_SUCCESS,
    STATUS_DUPLICATE_HASH,
    STATUS_DUPLICATE_FILENAME,
    STATUS_SKIPPED_FILTER,
    STATUS_SKIPPED_HEIC,
    STATUS_ERROR_MISSING_DATA, # Treat missing data as terminal unless manually reset
]

# List of statuses indicating an error occurred (will now be retried by default)
ERROR_STATUSES = [
    STATUS_ERROR_DOWNLOAD,
    STATUS_ERROR_HASHING,
    STATUS_ERROR_SMUGMUG_API,
    STATUS_ERROR_UPLOAD_FAILED,
    STATUS_ERROR_ALBUM_FULL,
    STATUS_ERROR_QUOTA, # Add new status here for retry (or maybe not if we exit?) - Added for now
    STATUS_ERROR_UNKNOWN,
    # STATUS_ERROR_MISSING_DATA is excluded here as it's less likely to be auto-resolved
]


class DatabaseManager:
    """Manages the SQLite database for transfer state."""

    def __init__(self, db_file=DB_FILE_DEFAULT):
        """Initializes the DatabaseManager."""
        self.db_file = db_file
        self.conn = None
        self._connect()
        self._create_tables() # Create tables if they don't exist

    def _connect(self):
        """Establishes a connection to the SQLite database."""
        try:
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False,
                                        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL;")
            logger.info(f"Connected to database: {self.db_file}")
        except sqlite3.Error as e:
            logger.critical(f"Error connecting to database {self.db_file}: {e}", exc_info=True)
            raise

    def _create_tables(self):
        """Creates the database tables if they don't exist."""
        if not self.conn:
            logger.error("Cannot create tables: No database connection.")
            return
        try:
            with self.conn:
                # Media Items Table (No changes needed here)
                self.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {MEDIA_TABLE_NAME} (
                        google_id TEXT PRIMARY KEY NOT NULL,
                        filename TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        base_url TEXT,
                        product_url TEXT,
                        creation_timestamp TEXT,
                        media_metadata_json TEXT,
                        status TEXT NOT NULL DEFAULT '{STATUS_PENDING}',
                        md5_hash TEXT,
                        smugmug_album_key TEXT,
                        last_error TEXT,
                        last_processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        upload_attempts INTEGER DEFAULT 0
                    )
                """)
                logger.debug(f"Ensured table '{MEDIA_TABLE_NAME}' exists.")
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_status ON {MEDIA_TABLE_NAME} (status);")
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_filename ON {MEDIA_TABLE_NAME} (filename);")
                logger.debug(f"Ensured indexes exist on '{MEDIA_TABLE_NAME}'.")

                # Run Config Table (No changes needed here)
                self.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {CONFIG_TABLE_NAME} (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        initial_run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        initial_album_name TEXT,
                        current_album_key TEXT NOT NULL,
                        current_album_uri TEXT NOT NULL,
                        current_folder_name TEXT,
                        google_album_id TEXT
                    )
                """)
                logger.debug(f"Ensured table '{CONFIG_TABLE_NAME}' exists.")

                # Schema Migration (No changes needed here)
                cursor = self.conn.execute(f"PRAGMA table_info({CONFIG_TABLE_NAME})")
                columns = [column[1] for column in cursor.fetchall()]
                if 'initial_album_name' not in columns:
                    logger.warning(f"Adding missing 'initial_album_name' column to {CONFIG_TABLE_NAME} table.")
                    self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} ADD COLUMN initial_album_name TEXT")
                if 'smugmug_album_key' in columns and 'current_album_key' not in columns:
                     logger.warning(f"Renaming 'smugmug_album_key' to 'current_album_key' in {CONFIG_TABLE_NAME}.")
                     self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} RENAME COLUMN smugmug_album_key TO current_album_key")
                if 'smugmug_album_uri' in columns and 'current_album_uri' not in columns:
                     logger.warning(f"Renaming 'smugmug_album_uri' to 'current_album_uri' in {CONFIG_TABLE_NAME}.")
                     self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} RENAME COLUMN smugmug_album_uri TO current_album_uri")
                if 'smugmug_folder_name' in columns and 'current_folder_name' not in columns:
                     logger.warning(f"Renaming 'smugmug_folder_name' to 'current_folder_name' in {CONFIG_TABLE_NAME}.")
                     self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} RENAME COLUMN smugmug_folder_name TO current_folder_name")

        except sqlite3.Error as e:
            logger.error(f"Error creating/updating tables: {e}", exc_info=True)
            raise

    def add_item_batch(self, items, target_album_key):
        """Adds a batch of media items to the database, skipping duplicates."""
        if not self.conn:
            logger.error("Cannot add items: No database connection.")
            return 0
        added_count = 0
        skipped_count = 0
        sql = f"""
            INSERT OR IGNORE INTO {MEDIA_TABLE_NAME} (
                google_id, filename, mime_type, base_url, product_url,
                creation_timestamp, media_metadata_json, status, smugmug_album_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        items_to_insert = []
        for item in items:
            google_id = item.get('id')
            if not google_id: logger.warning("Skipping item missing ID."); continue
            filename = item.get('filename')
            mime_type = item.get('mimeType')
            base_url = item.get('baseUrl')
            product_url = item.get('productUrl')
            media_metadata = item.get('mediaMetadata', {})
            creation_time = media_metadata.get('creationTime')
            media_metadata_json = json.dumps(media_metadata) if media_metadata else None
            if not filename or not mime_type: logger.warning(f"Skipping {google_id}: missing filename/mimeType."); continue
            items_to_insert.append((google_id, filename, mime_type, base_url, product_url, creation_time, media_metadata_json, STATUS_PENDING, target_album_key))
        if not items_to_insert: logger.info("No valid items in batch."); return 0
        try:
            with self.conn: cursor = self.conn.executemany(sql, items_to_insert)
            added_count = cursor.rowcount
            skipped_count = len(items_to_insert) - added_count
            logger.info(f"Batch add: Added {added_count}, Skipped {skipped_count} existing.")
            return added_count
        except sqlite3.Error as e: logger.error(f"Error adding batch: {e}", exc_info=True); return 0

    def update_item_status(self, google_id, status, error_message=None, md5_hash=None, increment_attempt=False):
        """Updates the status and optionally the error message or hash of a media item."""
        if not self.conn: logger.error(f"Update status fail {google_id}: No DB conn."); return False
        now_timestamp = datetime.datetime.now()
        base_sql = f"UPDATE {MEDIA_TABLE_NAME} SET status = ?, last_processed_timestamp = ?"
        params = [status, now_timestamp]
        if error_message is not None: base_sql += ", last_error = ?"; params.append(error_message)
        elif status not in ERROR_STATUSES and status != STATUS_ERROR_MISSING_DATA and status != STATUS_ERROR_ALBUM_FULL: base_sql += ", last_error = NULL" # Clear error on success/skip
        if md5_hash is not None: base_sql += ", md5_hash = ?"; params.append(md5_hash)
        if increment_attempt: base_sql += ", upload_attempts = upload_attempts + 1"
        base_sql += " WHERE google_id = ?"; params.append(google_id)
        try:
            with self.conn: self.conn.execute(base_sql, params)
            return True
        except sqlite3.Error as e:
            logger.error(f"FAILED SQL: {base_sql}")
            logger.error(f"FAILED PARAMS: {params}")
            logger.error(f"Error updating status for {google_id} to {status}: {e}", exc_info=True)
            return False

    def update_item_details(self, google_id, base_url, media_metadata_json):
        """Updates the base_url and media_metadata_json for an item."""
        if not self.conn: logger.error(f"Update details fail {google_id}: No DB conn."); return False
        now_timestamp = datetime.datetime.now()
        sql = f"UPDATE {MEDIA_TABLE_NAME} SET base_url = ?, media_metadata_json = ?, last_processed_timestamp = ? WHERE google_id = ?"
        params = [base_url, media_metadata_json, now_timestamp, google_id]
        try:
            with self.conn: self.conn.execute(sql, params)
            logger.debug(f"Updated details for {google_id}")
            return True
        except sqlite3.Error as e: logger.error(f"Error updating details for {google_id}: {e}", exc_info=True); return False

    def get_items_to_process(self):
        """Retrieves all items that are not in a terminal success/skip state."""
        if not self.conn: logger.error("Cannot get items: No DB conn."); return []
        terminal_placeholders = ', '.join('?' * len(TERMINAL_STATUSES))
        sql = f"SELECT * FROM {MEDIA_TABLE_NAME} WHERE status NOT IN ({terminal_placeholders})"
        try:
            cursor = self.conn.execute(sql, TERMINAL_STATUSES)
            items = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Retrieved {len(items)} items for processing.")
            return items
        except sqlite3.Error as e: logger.error(f"Error retrieving items: {e}", exc_info=True); return []

    def get_item_count(self):
        """Returns the total number of items in the media items table."""
        if not self.conn: return 0
        try: cursor = self.conn.execute(f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME}"); return cursor.fetchone()[0]
        except sqlite3.Error as e: logger.error(f"Error getting count: {e}", exc_info=True); return 0

    def get_stats(self):
        """Returns a dictionary with counts for each status."""
        if not self.conn: return {}
        stats = {}
        try:
            cursor = self.conn.execute(f"SELECT status, COUNT(*) FROM {MEDIA_TABLE_NAME} GROUP BY status")
            for row in cursor.fetchall(): stats[row['status']] = row['COUNT(*)']
            return stats
        except sqlite3.Error as e: logger.error(f"Error getting stats: {e}", exc_info=True); return {}

    def get_item_count_by_status(self, status_list):
        """Returns the count of items matching any status in the provided list."""
        if not self.conn or not status_list: return 0
        try:
            placeholders = ', '.join('?' * len(status_list))
            sql = f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME} WHERE status IN ({placeholders})"
            cursor = self.conn.execute(sql, status_list); return cursor.fetchone()[0]
        except sqlite3.Error as e: logger.error(f"Error getting count for {status_list}: {e}", exc_info=True); return 0

    def save_config_snapshot(self, initial_album_name, album_key, album_uri, folder_name, google_album_id):
        """Saves or updates the target configuration snapshot in the database."""
        if not self.conn: logger.error("Cannot save config: No DB conn."); return False
        sql = f"""INSERT OR REPLACE INTO {CONFIG_TABLE_NAME} (id, initial_album_name, current_album_key, current_album_uri, current_folder_name, google_album_id, initial_run_timestamp) VALUES (1, ?, ?, ?, ?, ?, ?)"""
        now_timestamp = datetime.datetime.now()
        params = [initial_album_name, album_key, album_uri, folder_name, google_album_id, now_timestamp]
        try:
            with self.conn: self.conn.execute(sql, params)
            logger.info(f"Saved/Updated config snapshot: Base='{initial_album_name}', Key='{album_key}', Folder='{folder_name}', Source='{google_album_id or 'Library'}'")
            return True
        except sqlite3.Error as e: logger.error(f"Error saving config snapshot: {e}", exc_info=True); return False

    def get_config_snapshot(self):
        """Retrieves the stored configuration snapshot from the database."""
        if not self.conn: logger.error("Cannot get stored config: No DB conn."); return None
        try:
            cursor = self.conn.execute(f"SELECT * FROM {CONFIG_TABLE_NAME} WHERE id = 1")
            row = cursor.fetchone()
            if row: logger.debug("Retrieved stored config snapshot."); return dict(row)
            else: logger.debug("No stored config snapshot found."); return None
        except sqlite3.Error as e: logger.error(f"Error retrieving stored config: {e}", exc_info=True); return None

    def reset_failed_items(self):
        """Resets all items with an error status back to PENDING."""
        if not self.conn: logger.error("Cannot reset items: No DB conn."); return 0
        logger.warning("Resetting items with error statuses back to PENDING...")
        error_status_list = list(set(ERROR_STATUSES))
        error_status_placeholders = ', '.join('?' * len(error_status_list))
        now_timestamp = datetime.datetime.now()
        sql = f"UPDATE {MEDIA_TABLE_NAME} SET status = ?, last_error = NULL, upload_attempts = 0, last_processed_timestamp = ? WHERE status IN ({error_status_placeholders})"
        params = [STATUS_PENDING, now_timestamp] + error_status_list
        try:
            with self.conn: cursor = self.conn.execute(sql, params)
            reset_count = cursor.rowcount
            logger.info(f"Reset {reset_count} items from error states to PENDING.")
            return reset_count
        except sqlite3.Error as e: logger.error(f"Error resetting failed items: {e}", exc_info=True); return 0

    def close(self):
        """Closes the database connection."""
        if self.conn:
            try: self.conn.close(); logger.info("DB connection closed."); self.conn = None
            except sqlite3.Error as e: logger.error(f"Error closing DB connection: {e}", exc_info=True)

    def __del__(self):
        """Ensure connection is closed when the object is garbage collected."""
        if self.conn: logger.warning("DBManager deleted without explicit close. Closing now."); self.close()

