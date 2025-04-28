# database_manager.py (v2.0)
# - Added get_item_count_by_status method.
# - Modified get_items_to_process to always include errors for retry.
# - Removed retry_errors parameter from get_items_to_process.
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
CONFIG_TABLE_NAME = "run_config" # New table for config snapshot

# --- Status Codes ---
# (Status codes remain the same)
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
    STATUS_ERROR_MISSING_DATA, # Treat missing data as terminal unless manually reset
]

# List of statuses indicating an error occurred (will now be retried by default)
ERROR_STATUSES = [
    STATUS_ERROR_DOWNLOAD,
    STATUS_ERROR_HASHING,
    STATUS_ERROR_SMUGMUG_API,
    STATUS_ERROR_UPLOAD_FAILED,
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
        self._create_tables() # Updated to create both tables

    def _connect(self):
        """Establishes a connection to the SQLite database."""
        try:
            # check_same_thread=False is necessary for multi-threaded access
            self.conn = sqlite3.connect(self.db_file, check_same_thread=False,
                                        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
            self.conn.row_factory = sqlite3.Row
            # Enable Write-Ahead Logging for better concurrency
            self.conn.execute("PRAGMA journal_mode=WAL;")
            logger.info(f"Connected to database: {self.db_file}")
        except sqlite3.Error as e:
            logger.critical(f"Error connecting to database {self.db_file}: {e}", exc_info=True)
            raise

    def _create_tables(self): # Renamed from _create_table
        """Creates the database tables if they don't exist."""
        if not self.conn:
            logger.error("Cannot create tables: No database connection.")
            return
        try:
            with self.conn:
                # --- Media Items Table ---
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
                        smugmug_album_key TEXT, -- Store the key used *for this item*
                        last_error TEXT,
                        last_processed_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        upload_attempts INTEGER DEFAULT 0
                    )
                """)
                logger.debug(f"Ensured table '{MEDIA_TABLE_NAME}' exists.")
                # Add indexes for faster queries
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_status ON {MEDIA_TABLE_NAME} (status);")
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_filename ON {MEDIA_TABLE_NAME} (filename);")
                logger.debug(f"Ensured indexes exist on '{MEDIA_TABLE_NAME}'.")

                # --- Run Config Table (New) ---
                # Stores the configuration used when the DB was first populated
                self.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {CONFIG_TABLE_NAME} (
                        id INTEGER PRIMARY KEY CHECK (id = 1), -- Enforce only one row
                        initial_run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        smugmug_album_key TEXT NOT NULL,
                        smugmug_album_uri TEXT NOT NULL,
                        smugmug_folder_name TEXT, -- Can be NULL if root
                        google_album_id TEXT -- Can be NULL if whole library
                    )
                """)
                logger.debug(f"Ensured table '{CONFIG_TABLE_NAME}' exists.")

        except sqlite3.Error as e:
            logger.error(f"Error creating tables: {e}", exc_info=True)

    def save_initial_config(self, sm_album_key, sm_album_uri, sm_folder, gp_album_id):
        """
        Saves the initial run configuration snapshot to the database.
        Only inserts if the config table is empty. Returns True on success/already exists, False on error.
        """
        if not self.conn: return False
        logger.info("Attempting to save initial run configuration snapshot to database...")
        # Check if config already exists
        try:
            cursor = self.conn.execute(f"SELECT COUNT(*) FROM {CONFIG_TABLE_NAME}")
            count = cursor.fetchone()[0]
            if count > 0:
                logger.info("Initial run configuration already exists in the database. Skipping save.")
                return True # Config already saved
        except sqlite3.Error as e:
            logger.error(f"Error checking existing run configuration: {e}", exc_info=True)
            return False # Error checking

        # Insert the initial config
        sql = f"""
            INSERT INTO {CONFIG_TABLE_NAME} (
                id, smugmug_album_key, smugmug_album_uri, smugmug_folder_name, google_album_id
            ) VALUES (?, ?, ?, ?, ?)
        """
        params = (1, sm_album_key, sm_album_uri, sm_folder, gp_album_id)
        try:
            with self.conn:
                self.conn.execute(sql, params)
            logger.info("Successfully saved initial run configuration snapshot.")
            logger.info(f"  - SmugMug Album Key: {sm_album_key}")
            logger.info(f"  - SmugMug Album URI: {sm_album_uri}")
            logger.info(f"  - SmugMug Folder: {sm_folder or 'Root'}")
            logger.info(f"  - Google Album ID: {gp_album_id or 'Entire Library'}")
            return True
        except sqlite3.IntegrityError:
            # This might happen in a race condition, although unlikely for this script.
            # Treat it as success because the config exists.
            logger.warning("Attempted to save initial config, but it seems to already exist (IntegrityError).")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error saving initial run configuration: {e}", exc_info=True)
            return False

    def get_stored_config(self):
        """
        Retrieves the stored initial run configuration snapshot.
        Returns a dictionary (or None if not found or error).
        """
        if not self.conn: return None
        sql = f"SELECT * FROM {CONFIG_TABLE_NAME} WHERE id = 1"
        try:
            cursor = self.conn.execute(sql)
            row = cursor.fetchone()
            if row:
                logger.debug("Retrieved stored run configuration snapshot from database.")
                return dict(row)
            else:
                logger.debug("No stored run configuration snapshot found in database.")
                return None
        except sqlite3.Error as e:
            logger.error(f"Error retrieving stored run configuration: {e}", exc_info=True)
            return None

    def add_item_batch(self, items, smugmug_album_key):
        """Adds a batch of items fetched from Google Photos to the database."""
        if not self.conn: return 0
        added_count = 0
        # Use MEDIA_TABLE_NAME
        sql = f"""
            INSERT OR IGNORE INTO {MEDIA_TABLE_NAME} (
                google_id, filename, mime_type, base_url, product_url,
                creation_timestamp, media_metadata_json, smugmug_album_key, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        items_to_insert = []
        for item in items:
            # Basic validation for essential fields
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
                json.dumps(metadata), # Store metadata as JSON string
                smugmug_album_key, # Store the key used for this batch
                STATUS_PENDING # Initial status
            ))

        if not items_to_insert:
            logger.warning("No valid items provided in the batch to add to the database.")
            return 0

        try:
            with self.conn:
                cursor = self.conn.executemany(sql, items_to_insert)
                added_count = cursor.rowcount # Number of rows actually inserted (ignores duplicates)
            logger.info(f"Added/Ignored {len(items_to_insert)} items in batch. New rows inserted: {added_count}")
            return added_count
        except sqlite3.Error as e:
            logger.error(f"Error adding item batch to database: {e}", exc_info=True)
            return 0

    # --- Modified get_items_to_process ---
    def get_items_to_process(self, limit=None):
        """
        Gets items that need processing based on status.
        ALWAYS includes items with retryable error statuses.
        """
        if not self.conn: return []

        # Define statuses indicating item needs processing or retry
        statuses_to_fetch = [
            STATUS_PENDING,
            STATUS_HASHED,
            STATUS_SMUGMUG_CHECKED_NOT_FOUND,
            STATUS_DOWNLOADED_FOR_UPLOAD,
            STATUS_UPLOAD_ATTEMPTED,
        ]
        # Always include retryable errors
        statuses_to_fetch.extend(ERROR_STATUSES)
        # Remove duplicates just in case
        statuses_to_fetch = list(set(statuses_to_fetch))

        logger.info(f"Querying for items to process (including errors) with statuses: {statuses_to_fetch}")

        placeholders = ', '.join('?' * len(statuses_to_fetch))
        # Use MEDIA_TABLE_NAME
        sql = f"SELECT * FROM {MEDIA_TABLE_NAME} WHERE status IN ({placeholders}) ORDER BY creation_timestamp ASC"

        # Apply limit if provided
        if limit and isinstance(limit, int) and limit > 0:
            sql += f" LIMIT {limit}"

        try:
            cursor = self.conn.execute(sql, statuses_to_fetch)
            items = cursor.fetchall()
            # Convert sqlite3.Row objects to dictionaries for easier handling
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

        now_timestamp = datetime.datetime.now()

        # Build the SET part of the SQL query dynamically
        sql_parts = ["status = ?", "last_processed_timestamp = ?"]
        params = [status, now_timestamp]

        if error_message is not None:
            sql_parts.append("last_error = ?")
            params.append(error_message)
        elif status not in ERROR_STATUSES:
             # Clear last_error if the new status is not an error
             sql_parts.append("last_error = NULL")

        if md5_hash is not None:
            sql_parts.append("md5_hash = ?")
            params.append(md5_hash)

        if increment_attempt:
             # Increment upload_attempts counter
             sql_parts.append("upload_attempts = upload_attempts + 1")

        # Use MEDIA_TABLE_NAME
        sql = f"UPDATE {MEDIA_TABLE_NAME} SET {', '.join(sql_parts)} WHERE google_id = ?"
        params.append(google_id)

        try:
            with self.conn:
                cursor = self.conn.execute(sql, params)
            if cursor.rowcount == 0:
                 # Log a warning if no rows were updated (e.g., google_id not found)
                 logger.warning(f"No rows updated for google_id {google_id} during status update.")
                 return False
            return True
        except sqlite3.Error as e:
            logger.error(f"Error updating status for google_id {google_id}: {e}", exc_info=True)
            return False

    def update_item_details(self, google_id, base_url, media_metadata_json):
        """Updates the baseUrl and metadata for an item, typically after a refresh."""
        if not self.conn or not google_id: return False
        logger.debug(f"Updating DB details for {google_id}: New BaseUrl, New Metadata")

        now_timestamp = datetime.datetime.now()
        # Use MEDIA_TABLE_NAME
        sql = f"""
            UPDATE {MEDIA_TABLE_NAME}
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

    def get_item_count(self, status=None):
        """Gets the total count of items, or count for a specific status."""
        if not self.conn: return 0
        try:
            # Use MEDIA_TABLE_NAME
            if status:
                # Query count for a specific status
                cursor = self.conn.execute(f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME} WHERE status = ?", (status,))
            else:
                # Query total count
                cursor = self.conn.execute(f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME}")
            count = cursor.fetchone()[0]
            return count
        except sqlite3.Error as e:
            logger.error(f"Error getting item count (status: {status}): {e}", exc_info=True)
            return 0

    # --- NEW Method ---
    def get_item_count_by_status(self, statuses):
        """Gets the count of items matching any status in the provided list."""
        if not self.conn or not statuses: return 0
        try:
            # Create placeholders for the IN clause
            placeholders = ', '.join('?' * len(statuses))
            # Use MEDIA_TABLE_NAME
            sql = f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME} WHERE status IN ({placeholders})"
            cursor = self.conn.execute(sql, statuses)
            count = cursor.fetchone()[0]
            logger.debug(f"Count for statuses {statuses}: {count}")
            return count
        except sqlite3.Error as e:
            logger.error(f"Error getting item count for statuses {statuses}: {e}", exc_info=True)
            return 0

    def get_stats(self):
        """Returns a dictionary with counts for various statuses."""
        if not self.conn: return {}
        stats = {}
        try:
            # Use MEDIA_TABLE_NAME
            cursor = self.conn.execute(f"SELECT status, COUNT(*) FROM {MEDIA_TABLE_NAME} GROUP BY status")
            rows = cursor.fetchall()
            for row in rows:
                stats[row['status']] = row['COUNT(*)']
            # Ensure all known statuses have a count, even if 0
            all_statuses = list(set([
                STATUS_PENDING, STATUS_HASHED, STATUS_SMUGMUG_CHECKED_NOT_FOUND,
                STATUS_DOWNLOADED_FOR_UPLOAD, STATUS_UPLOAD_ATTEMPTED, STATUS_UPLOADED_SUCCESS,
                STATUS_DUPLICATE_HASH, STATUS_DUPLICATE_FILENAME, STATUS_SKIPPED_FILTER,
                STATUS_SKIPPED_HEIC] + ERROR_STATUSES + [STATUS_ERROR_MISSING_DATA])) # Include all possible statuses
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
              # Use MEDIA_TABLE_NAME
              cursor = self.conn.execute(f"SELECT * FROM {MEDIA_TABLE_NAME} WHERE google_id = ?", (google_id,))
              row = cursor.fetchone()
              return dict(row) if row else None
         except sqlite3.Error as e:
              logger.error(f"Error getting details for google_id {google_id}: {e}", exc_info=True)
              return None

    def reset_failed_items(self):
        """Resets items with error statuses back to PENDING for retry."""
        if not self.conn: return 0
        logger.warning("Resetting items with error statuses back to PENDING...")
        # Include all defined error statuses in the reset list
        error_status_list = list(set(ERROR_STATUSES + [STATUS_ERROR_MISSING_DATA]))
        error_status_placeholders = ', '.join('?' * len(error_status_list))
        now_timestamp = datetime.datetime.now()
        # Use MEDIA_TABLE_NAME
        sql = f"""
            UPDATE {MEDIA_TABLE_NAME}
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
                # WAL mode benefits from an explicit checkpoint before closing
                # although Python's sqlite3 module might handle this implicitly.
                # self.conn.execute("PRAGMA wal_checkpoint(FULL);")
                self.conn.close()
                logger.info("Database connection closed.")
                self.conn = None
            except sqlite3.Error as e:
                logger.error(f"Error closing database connection: {e}", exc_info=True)

    def __del__(self):
        """Ensure connection is closed when object is deleted."""
        self.close()
