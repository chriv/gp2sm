# database_manager.py (v1.9 - Added update_item_details)
# Handles SQLite database operations for gp2sm transfer state.

import sqlite3
import logging
import os
import json # To store complex metadata if needed
import datetime # For timestamp updates

logger = logging.getLogger(__name__)

# --- Constants ---
DB_FILE_DEFAULT = "gp2sm_transfer_state.db"
TABLE_NAME = "media_items"

# --- Status Codes ---
# Using strings for better readability in the DB
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
STATUS_ERROR_UNKNOWN = "ERROR_UNKNOWN"
STATUS_ERROR_MISSING_DATA = "ERROR_MISSING_DATA" # Item lacks essential fields

# List of terminal success/skip statuses (won't be retried by default)
TERMINAL_STATUSES = [
    STATUS_UPLOADED_SUCCESS,
    STATUS_DUPLICATE_HASH,
    STATUS_DUPLICATE_FILENAME,
    STATUS_SKIPPED_FILTER,
    STATUS_SKIPPED_HEIC,
    STATUS_ERROR_MISSING_DATA,
]

# List of statuses indicating an error occurred
ERROR_STATUSES = [
    STATUS_ERROR_DOWNLOAD,
    STATUS_ERROR_HASHING,
    STATUS_ERROR_SMUGMUG_API,
    STATUS_ERROR_UPLOAD_FAILED,
    STATUS_ERROR_UNKNOWN,
    STATUS_ERROR_MISSING_DATA,
]


class DatabaseManager:
    """Manages the SQLite database for transfer state."""

    def __init__(self, db_file=DB_FILE_DEFAULT):
        """Initializes the DatabaseManager."""
        self.db_file = db_file
        self.conn = None
        self._connect()
        self._create_table()

    def _connect(self):
        """Establishes a connection to the SQLite database."""
        try:
            # Using check_same_thread=False is generally okay for CLI tools,
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False,
                                        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES) # Enable type detection
            self.conn.row_factory = sqlite3.Row
            # Enable Write-Ahead Logging for potentially better concurrency (though less relevant for CLI)
            self.conn.execute("PRAGMA journal_mode=WAL;")
            logger.info(f"Connected to database: {self.db_file}")
        except sqlite3.Error as e:
            logger.critical(f"Error connecting to database {self.db_file}: {e}", exc_info=True)
            raise

    def _create_table(self):
        """Creates the media_items table if it doesn't exist."""
        if not self.conn:
            logger.error("Cannot create table: No database connection.")
            return
        try:
            with self.conn:
                self.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                        google_id TEXT PRIMARY KEY NOT NULL,
                        filename TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        base_url TEXT,
                        product_url TEXT,
                        creation_timestamp TEXT, -- Store as ISO 8601 string
                        media_metadata_json TEXT,
                        status TEXT NOT NULL DEFAULT '{STATUS_PENDING}',
                        md5_hash TEXT,
                        smugmug_album_key TEXT,
                        last_error TEXT,
                        -- Use TIMESTAMP type and CURRENT_TIMESTAMP default
                        last_processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        upload_attempts INTEGER DEFAULT 0
                    )
                """)
                logger.debug(f"Ensured table '{TABLE_NAME}' exists.")
                # Add indexes
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_status ON {TABLE_NAME} (status);")
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_filename ON {TABLE_NAME} (filename);")
                logger.debug(f"Ensured indexes exist on '{TABLE_NAME}'.")
        except sqlite3.Error as e:
            logger.error(f"Error creating table or indexes '{TABLE_NAME}': {e}", exc_info=True)

    def add_item_batch(self, items, smugmug_album_key):
        """Adds a batch of items fetched from Google Photos to the database."""
        if not self.conn: return 0
        added_count = 0
        sql = f"""
            INSERT OR IGNORE INTO {TABLE_NAME} (
                google_id, filename, mime_type, base_url, product_url,
                creation_timestamp, media_metadata_json, smugmug_album_key, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        items_to_insert = []
        for item in items:
            if not isinstance(item, dict) or not item.get('id') or not item.get('filename') or not item.get('mimeType'):
                 logger.warning(f"Skipping invalid item during DB insert: {item}")
                 continue

            metadata = item.get('mediaMetadata', {})
            creation_time = metadata.get('creationTime') # Keep as string from API

            items_to_insert.append((
                item['id'],
                item['filename'],
                item['mimeType'],
                item.get('baseUrl'),
                item.get('productUrl'),
                creation_time,
                json.dumps(metadata), # Store metadata as JSON
                smugmug_album_key,
                STATUS_PENDING
            ))

        if not items_to_insert:
            logger.warning("No valid items provided in the batch to add to the database.")
            return 0

        try:
            with self.conn:
                cursor = self.conn.executemany(sql, items_to_insert)
                added_count = cursor.rowcount
            logger.info(f"Added/Ignored {len(items_to_insert)} items in batch. Rows inserted: {added_count}")
            return added_count
        except sqlite3.Error as e:
            logger.error(f"Error adding item batch to database: {e}", exc_info=True)
            return 0

    def get_items_to_process(self, limit=None, retry_errors=False):
        """Gets items that need processing based on status."""
        if not self.conn: return []

        statuses_to_fetch = [
            STATUS_PENDING, STATUS_HASHED, STATUS_SMUGMUG_CHECKED_NOT_FOUND,
            STATUS_DOWNLOADED_FOR_UPLOAD, STATUS_UPLOAD_ATTEMPTED,
        ]

        if retry_errors:
            retryable_errors = [
                STATUS_ERROR_DOWNLOAD, STATUS_ERROR_HASHING, STATUS_ERROR_SMUGMUG_API,
                STATUS_ERROR_UPLOAD_FAILED, STATUS_ERROR_UNKNOWN,
            ]
            statuses_to_fetch.extend(retryable_errors)
            logger.info("Querying for items to process, including retryable errors.")
        else:
             logger.info("Querying for items to process (excluding errors).")

        placeholders = ', '.join('?' * len(statuses_to_fetch))
        # Order by creation time to process older items first
        sql = f"SELECT * FROM {TABLE_NAME} WHERE status IN ({placeholders}) ORDER BY creation_timestamp ASC"

        if limit and isinstance(limit, int) and limit > 0:
            sql += f" LIMIT {limit}"

        try:
            cursor = self.conn.execute(sql, statuses_to_fetch)
            items = cursor.fetchall()
            item_dicts = [dict(row) for row in items]
            logger.info(f"Found {len(item_dicts)} items to process.")
            return item_dicts
        except sqlite3.Error as e:
            logger.error(f"Error fetching items to process: {e}", exc_info=True)
            return []

    def update_item_status(self, google_id, status, error_message=None, md5_hash=None, increment_attempt=False):
        """Updates the status and optionally other fields for a specific item."""
        if not self.conn or not google_id: return False
        logger.debug(f"Updating DB status for {google_id}: Status='{status}', Error='{error_message}', MD5='{md5_hash}', IncrAttempt={increment_attempt}")

        # Use datetime.datetime.now() for Python-generated timestamp
        now_timestamp = datetime.datetime.now()

        sql_parts = ["status = ?", "last_processed_timestamp = ?"]
        params = [status, now_timestamp] # Pass timestamp as parameter

        if error_message is not None:
            sql_parts.append("last_error = ?")
            params.append(error_message)
        elif status not in ERROR_STATUSES:
             sql_parts.append("last_error = NULL") # Clear error if status is not an error

        if md5_hash is not None:
            sql_parts.append("md5_hash = ?")
            params.append(md5_hash)

        if increment_attempt:
             sql_parts.append("upload_attempts = upload_attempts + 1")

        sql = f"UPDATE {TABLE_NAME} SET {', '.join(sql_parts)} WHERE google_id = ?"
        params.append(google_id)

        try:
            with self.conn:
                cursor = self.conn.execute(sql, params)
            if cursor.rowcount == 0:
                 logger.warning(f"No rows updated for google_id {google_id} during status update.")
                 return False
            return True
        except sqlite3.Error as e:
            logger.error(f"Error updating status for google_id {google_id}: {e}", exc_info=True)
            return False

    # --- New Method ---
    def update_item_details(self, google_id, base_url, media_metadata_json):
        """Updates the baseUrl and metadata for an item, typically after a refresh."""
        if not self.conn or not google_id: return False
        logger.debug(f"Updating DB details for {google_id}: New BaseUrl, New Metadata")

        now_timestamp = datetime.datetime.now()
        sql = f"""
            UPDATE {TABLE_NAME}
            SET base_url = ?, media_metadata_json = ?, last_processed_timestamp = ?
            WHERE google_id = ?
        """
        params = [base_url, media_metadata_json, now_timestamp, google_id]

        try:
            with self.conn:
                cursor = self.conn.execute(sql, params)
            if cursor.rowcount == 0:
                 logger.warning(f"No rows updated for google_id {google_id} during detail update.")
                 return False
            logger.info(f"Successfully updated details (baseUrl, metadata) for google_id {google_id} in DB.")
            return True
        except sqlite3.Error as e:
             logger.error(f"Error updating details for google_id {google_id}: {e}", exc_info=True)
             return False
    # --- End New Method ---

    def get_item_count(self, status=None):
        """Gets the total count of items, or count for a specific status."""
        if not self.conn: return 0
        try:
            if status:
                cursor = self.conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME} WHERE status = ?", (status,))
            else:
                cursor = self.conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
            count = cursor.fetchone()[0]
            return count
        except sqlite3.Error as e:
            logger.error(f"Error getting item count (status: {status}): {e}", exc_info=True)
            return 0

    def get_stats(self):
        """Returns a dictionary with counts for various statuses."""
        if not self.conn: return {}
        stats = {}
        try:
            cursor = self.conn.execute(f"SELECT status, COUNT(*) FROM {TABLE_NAME} GROUP BY status")
            rows = cursor.fetchall()
            for row in rows:
                stats[row['status']] = row['COUNT(*)']
            # Ensure all known statuses have a count, even if 0
            all_statuses = list(set([
                STATUS_PENDING, STATUS_HASHED, STATUS_SMUGMUG_CHECKED_NOT_FOUND,
                STATUS_DOWNLOADED_FOR_UPLOAD, STATUS_UPLOAD_ATTEMPTED, STATUS_UPLOADED_SUCCESS,
                STATUS_DUPLICATE_HASH, STATUS_DUPLICATE_FILENAME, STATUS_SKIPPED_FILTER,
                STATUS_SKIPPED_HEIC] + ERROR_STATUSES)) # Use set to handle potential duplicates
            for s in all_statuses:
                 if s not in stats:
                      stats[s] = 0
            stats['TOTAL'] = sum(stats.values())
            return stats
        except sqlite3.Error as e:
            logger.error(f"Error getting database stats: {e}", exc_info=True)
            return {}


    def get_item_details(self, google_id):
         """Gets full details for a single item from the database."""
         if not self.conn or not google_id: return None
         try:
              cursor = self.conn.execute(f"SELECT * FROM {TABLE_NAME} WHERE google_id = ?", (google_id,))
              row = cursor.fetchone()
              return dict(row) if row else None
         except sqlite3.Error as e:
              logger.error(f"Error getting details for google_id {google_id}: {e}", exc_info=True)
              return None

    def reset_failed_items(self):
        """Resets items with error statuses back to PENDING for retry."""
        if not self.conn: return 0
        logger.warning("Resetting items with error statuses back to PENDING...")
        # Create unique list of error statuses
        error_status_list = list(set(ERROR_STATUSES))
        error_status_placeholders = ', '.join('?' * len(error_status_list))
        now_timestamp = datetime.datetime.now()
        sql = f"""
            UPDATE {TABLE_NAME}
            SET status = ?, last_error = NULL, upload_attempts = 0, last_processed_timestamp = ?
            WHERE status IN ({error_status_placeholders})
        """
        params = [STATUS_PENDING, now_timestamp] + error_status_list
        try:
            with self.conn:
                cursor = self.conn.execute(sql, params)
            reset_count = cursor.rowcount
            logger.info(f"Reset {reset_count} items from error states to PENDING.")
            return reset_count
        except sqlite3.Error as e:
            logger.error(f"Error resetting failed items: {e}", exc_info=True)
            return 0

    def close(self):
        """Closes the database connection."""
        if self.conn:
            try:
                # Optional: Commit any final changes if not using autocommit
                # self.conn.commit()
                self.conn.close()
                logger.info("Database connection closed.")
                self.conn = None
            except sqlite3.Error as e:
                logger.error(f"Error closing database connection: {e}", exc_info=True)

    def __del__(self):
        """Ensure connection is closed when object is deleted."""
        self.close()
