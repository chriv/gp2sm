# database_manager.py (v2.0)
# - Added STATUS_ERROR_ALBUM_FULL status code and included in ERROR_STATUSES.
# - Added initial_album_name column to run_config table with migration logic.
# - Modified save_config_snapshot to use INSERT OR REPLACE and handle new column.
# - Modified get_config_snapshot to retrieve new column names.
# - Added smugmug_album_key parameter to update_item_status.
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
ALBUMS_TABLE_NAME = "smugmug_albums" # Table for tracking all albums

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
STATUS_SKIPPED_BMP = "SKIPPED_BMP"
STATUS_SKIPPED_WEBP = "SKIPPED_WEBP"
STATUS_SKIPPED_LARGE_VIDEO = "SKIPPED_LARGE_VIDEO"
STATUS_ERROR_DOWNLOAD = "ERROR_DOWNLOAD"
STATUS_ERROR_HASHING = "ERROR_HASHING"
STATUS_ERROR_SMUGMUG_API = "ERROR_SMUGMUG_API" # General SM API error during check/upload
STATUS_ERROR_UPLOAD_FAILED = "ERROR_UPLOAD_FAILED" # Upload POST failed
STATUS_ERROR_ALBUM_FULL = "ERROR_ALBUM_FULL" # SmugMug album limit reached
STATUS_ERROR_QUOTA = "ERROR_QUOTA" # Google API Quota Exceeded
STATUS_ERROR_UNKNOWN = "ERROR_UNKNOWN"
STATUS_ERROR_MISSING_DATA = "ERROR_MISSING_DATA" # Item lacks essential fields

# List of terminal success/skip statuses (won't be retried by default)
TERMINAL_STATUSES = [
    STATUS_UPLOADED_SUCCESS,
    STATUS_DUPLICATE_HASH,
    STATUS_DUPLICATE_FILENAME,
    STATUS_SKIPPED_FILTER,
    STATUS_SKIPPED_HEIC,
    STATUS_SKIPPED_BMP,
    STATUS_SKIPPED_WEBP,
    STATUS_SKIPPED_LARGE_VIDEO,
    STATUS_ERROR_MISSING_DATA, # Treat missing data as terminal unless manually reset
]

# List of statuses indicating an error occurred (will now be retried by default)
# Exclude QUOTA and ALBUM_FULL from automatic reset via --reset-errors if desired,
# but include them here so get_items_to_process picks them up for the *next* run.
ERROR_STATUSES = [
    STATUS_ERROR_DOWNLOAD,
    STATUS_ERROR_HASHING,
    STATUS_ERROR_SMUGMUG_API,
    STATUS_ERROR_UPLOAD_FAILED,
    STATUS_ERROR_ALBUM_FULL,
    STATUS_ERROR_QUOTA,
    STATUS_ERROR_UNKNOWN,
    # STATUS_ERROR_MISSING_DATA is excluded here as it's less likely to be auto-resolved
]

placeholders = ', '.join('?' * len(TERMINAL_STATUSES))

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

    def _create_tables(self):
        """Creates the database tables if they don't exist and handles schema migration."""
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
                        smugmug_album_key TEXT, -- Store the key used *for this item* (matches config snapshot)
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

                # --- Run Config Table ---
                # Stores the configuration used when the DB was first populated or last force-refreshed
                # Added initial_album_name to track the base name for sequential numbering
                self.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {CONFIG_TABLE_NAME} (
                        id INTEGER PRIMARY KEY CHECK (id = 1), -- Enforce only one row
                        initial_run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        initial_album_name TEXT,      -- Store the original base album name (NEW)
                        current_album_key TEXT NOT NULL, -- Renamed from smugmug_album_key
                        current_album_uri TEXT NOT NULL, -- Renamed from smugmug_album_uri
                        current_folder_name TEXT,    -- Renamed from smugmug_folder_name
                        google_album_id TEXT
                    )
                """)
                logger.debug(f"Ensured table '{CONFIG_TABLE_NAME}' exists.")

                # --- Schema Migration (Example: Add initial_album_name if missing) ---
                # Check if the new column exists and add it if not
                cursor = self.conn.execute(f"PRAGMA table_info({CONFIG_TABLE_NAME})")
                columns = [column[1] for column in cursor.fetchall()]
                if 'initial_album_name' not in columns:
                    logger.warning(f"Adding missing 'initial_album_name' column to {CONFIG_TABLE_NAME} table.")
                    self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} ADD COLUMN initial_album_name TEXT")
                # Rename old columns if they exist (for backward compatibility)
                if 'smugmug_album_key' in columns and 'current_album_key' not in columns:
                     logger.warning(f"Renaming 'smugmug_album_key' to 'current_album_key' in {CONFIG_TABLE_NAME}.")
                     self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} RENAME COLUMN smugmug_album_key TO current_album_key")
                if 'smugmug_album_uri' in columns and 'current_album_uri' not in columns:
                     logger.warning(f"Renaming 'smugmug_album_uri' to 'current_album_uri' in {CONFIG_TABLE_NAME}.")
                     self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} RENAME COLUMN smugmug_album_uri TO current_album_uri")
                if 'smugmug_folder_name' in columns and 'current_folder_name' not in columns:
                     logger.warning(f"Renaming 'smugmug_folder_name' to 'current_folder_name' in {CONFIG_TABLE_NAME}.")
                     self.conn.execute(f"ALTER TABLE {CONFIG_TABLE_NAME} RENAME COLUMN smugmug_folder_name TO current_folder_name")
                
                # --- Albums Tracking Table ---
                self.conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {ALBUMS_TABLE_NAME} (
                        album_key TEXT PRIMARY KEY NOT NULL,
                        album_name TEXT NOT NULL,
                        album_uri TEXT NOT NULL,
                        folder_name TEXT,
                        creation_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        item_count INTEGER DEFAULT 0,
                        is_current BOOLEAN DEFAULT 0
                    )
                """)
                logger.debug(f"Ensured table '{ALBUMS_TABLE_NAME}' exists.")
                # Add index for faster queries
                self.conn.execute(f"CREATE INDEX IF NOT EXISTS idx_is_current ON {ALBUMS_TABLE_NAME} (is_current);")
                logger.debug(f"Ensured indexes exist on '{ALBUMS_TABLE_NAME}'.")

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
            if not google_id:
                logger.warning("Skipping item with missing ID in batch add.")
                continue
            filename = item.get('filename')
            mime_type = item.get('mimeType')
            base_url = item.get('baseUrl')
            product_url = item.get('productUrl')
            media_metadata = item.get('mediaMetadata', {})
            creation_time = media_metadata.get('creationTime')
            media_metadata_json = json.dumps(media_metadata) if media_metadata else None
            if not filename or not mime_type:
                logger.warning(f"Skipping item {google_id} due to missing filename or mimeType.")
                continue
            items_to_insert.append((
                google_id, filename, mime_type, base_url, product_url,
                creation_time, media_metadata_json, STATUS_PENDING, target_album_key
            ))
        if not items_to_insert:
            logger.info("No valid items provided in batch to add.")
            return 0
        try:
            with self.conn:
                cursor = self.conn.executemany(sql, items_to_insert)
            added_count = cursor.rowcount
            skipped_count = len(items_to_insert) - added_count
            logger.info(f"Batch add complete: Added {added_count} new items, skipped {skipped_count} existing items.")
            return added_count
        except sqlite3.Error as e:
            logger.error(f"Error adding item batch to database: {e}", exc_info=True)
            return 0

    def update_item_status(self, google_id, status, error_message=None, md5_hash=None, increment_attempt=False, smugmug_album_key=None):
        """Updates the status and optionally the error message, hash, or target album key of a media item."""
        if not self.conn:
            logger.error(f"Cannot update status for {google_id}: No database connection.")
            return False

        now_timestamp = datetime.datetime.now()
        base_sql = f"UPDATE {MEDIA_TABLE_NAME} SET status = ?, last_processed_timestamp = ?"
        params = [status, now_timestamp]

        if error_message is not None:
            base_sql += ", last_error = ?"
            params.append(error_message)
        else:
            # Clear previous error if status is not an error status
            # Keep ALBUM_FULL error message even if retrying
            if status not in ERROR_STATUSES and status != STATUS_ERROR_MISSING_DATA:
                base_sql += ", last_error = NULL"

        if md5_hash is not None:
            base_sql += ", md5_hash = ?"
            params.append(md5_hash)

        if increment_attempt:
            base_sql += ", upload_attempts = upload_attempts + 1"

        # *** Add update for smugmug_album_key ***
        if smugmug_album_key is not None:
            base_sql += ", smugmug_album_key = ?"
            params.append(smugmug_album_key)

        base_sql += " WHERE google_id = ?"
        params.append(google_id)

        try:
            with self.conn:
                self.conn.execute(base_sql, params)
            # logger.debug(f"Updated status for {google_id} to {status}") # Can be noisy
            return True
        except sqlite3.Error as e:
            logger.error(f"FAILED SQL: {base_sql}")
            logger.error(f"FAILED PARAMS: {params}")
            logger.error(f"Error updating status for {google_id} to {status}: {e}", exc_info=True)
            return False

    def update_item_details(self, google_id, base_url, media_metadata_json):
        """Updates the base_url and media_metadata_json for an item."""
        if not self.conn:
            logger.error(f"Cannot update details for {google_id}: No database connection.")
            return False
        now_timestamp = datetime.datetime.now()
        sql = f"UPDATE {MEDIA_TABLE_NAME} SET base_url = ?, media_metadata_json = ?, last_processed_timestamp = ? WHERE google_id = ?"
        params = [base_url, media_metadata_json, now_timestamp, google_id]
        try:
            with self.conn:
                self.conn.execute(sql, params)
            logger.debug(f"Updated details for {google_id}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error updating details for {google_id}: {e}", exc_info=True)
            return False

    def get_items_to_process(self):
        """
        Retrieves all items that are not in a terminal success/skip state.
        Orders results so HEIC files come immediately after their JPG/JPEG counterparts.
        """
        if not self.conn:
            logger.error("Cannot get items: No database connection.")
            return []
        
        terminal_placeholders = ', '.join('?' * len(TERMINAL_STATUSES))
        
        # Modified SQL query with custom ORDER BY clause to group HEIC files with their JPG/JPEG counterparts
        sql = f"""
            SELECT * FROM {MEDIA_TABLE_NAME}
            WHERE status NOT IN ({terminal_placeholders})
            ORDER BY
                -- First sort by filename without extension
                SUBSTR(filename, 1, INSTR(LOWER(filename), '.') - 1),
                -- Then sort by extension with custom ordering (JPG/JPEG first, then HEIC, then others)
                CASE
                    WHEN LOWER(SUBSTR(filename, -4)) IN ('.jpg', 'jpeg') THEN 1
                    WHEN LOWER(SUBSTR(filename, -5)) = '.heic' THEN 2
                    ELSE 3
                END
        """
        
        try:
            cursor = self.conn.execute(sql, TERMINAL_STATUSES)
            items = [dict(row) for row in cursor.fetchall()]
            logger.info(f"Retrieved {len(items)} items for processing (excluding terminal statuses).")
            logger.debug("Items are ordered with HEIC files immediately after their JPG/JPEG counterparts.")
            return items
        except sqlite3.Error as e:
            logger.error(f"Error retrieving items to process: {e}", exc_info=True)
            return []

    def get_item_count(self):
        """Returns the total number of items in the media items table."""
        if not self.conn: return 0
        try:
            cursor = self.conn.execute(f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME}")
            return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"Error getting item count: {e}", exc_info=True)
            return 0

    def get_stats(self):
        """Returns a dictionary with counts for each status."""
        if not self.conn: return {}
        stats = {}
        try:
            cursor = self.conn.execute(f"SELECT status, COUNT(*) FROM {MEDIA_TABLE_NAME} GROUP BY status")
            for row in cursor.fetchall():
                stats[row['status']] = row['COUNT(*)']
            return stats
        except sqlite3.Error as e:
            logger.error(f"Error getting status stats: {e}", exc_info=True)
            return {}

    def get_item_count_by_status(self, status_list):
        """Returns the count of items matching any status in the provided list."""
        if not self.conn or not status_list: return 0
        try:
            placeholders = ', '.join('?' * len(status_list))
            sql = f"SELECT COUNT(*) FROM {MEDIA_TABLE_NAME} WHERE status IN ({placeholders})"
            cursor = self.conn.execute(sql, status_list)
            return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"Error getting item count for statuses {status_list}: {e}", exc_info=True)
            return 0

    def save_config_snapshot(self, initial_album_name, album_key, album_uri, folder_name, google_album_id):
        """Saves or updates the target configuration snapshot in the database."""
        if not self.conn:
            logger.error("Cannot save config: No database connection.")
            return False
        sql = f"""
            INSERT OR REPLACE INTO {CONFIG_TABLE_NAME}
            (id, initial_album_name, current_album_key, current_album_uri, current_folder_name, google_album_id, initial_run_timestamp)
            VALUES (1, ?, ?, ?, ?, ?, ?)
        """
        now_timestamp = datetime.datetime.now()
        params = [initial_album_name, album_key, album_uri, folder_name, google_album_id, now_timestamp]
        try:
            with self.conn:
                self.conn.execute(sql, params)
            logger.info(f"Saved/Updated config snapshot: Base='{initial_album_name}', Key='{album_key}', Folder='{folder_name}', Source='{google_album_id or 'Library'}'")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error saving config snapshot: {e}", exc_info=True)
            return False

    def get_config_snapshot(self):
        """Retrieves the stored configuration snapshot from the database."""
        if not self.conn:
            logger.error("Cannot get stored config: No database connection.")
            return None
        try:
            cursor = self.conn.execute(f"SELECT * FROM {CONFIG_TABLE_NAME} WHERE id = 1")
            row = cursor.fetchone()
            if row:
                logger.debug("Retrieved stored config snapshot.")
                return dict(row)
            else:
                logger.debug("No stored config snapshot found.")
                return None
        except sqlite3.Error as e:
            logger.error(f"Error retrieving stored config: {e}", exc_info=True)
            return None

    def reset_failed_items(self):
        """Resets all items with an error status back to PENDING."""
        if not self.conn:
            logger.error("Cannot reset items: No DB conn.")
            return 0
        logger.warning("Resetting items with error statuses back to PENDING...")
        error_status_list = list(set(ERROR_STATUSES))
        error_status_placeholders = ', '.join('?' * len(error_status_list))
        now_timestamp = datetime.datetime.now()
        sql = f"UPDATE {MEDIA_TABLE_NAME} SET status = ?, last_error = NULL, upload_attempts = 0, last_processed_timestamp = ? WHERE status IN ({error_status_placeholders})"
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

    def restart_all_items(self):
        """
        Resets ALL items to PENDING status, keeping MD5 hashes but clearing album assignments.
        This is for the restart/redo option (requirement 5).
        """
        if not self.conn:
            logger.error("Cannot restart items: No DB conn.")
            return 0
        
        logger.warning("Resetting ALL items to PENDING status, keeping MD5 hashes but clearing album assignments...")
        now_timestamp = datetime.datetime.now()
        
        sql = f"""
            UPDATE {MEDIA_TABLE_NAME}
            SET status = ?,
                last_error = NULL,
                upload_attempts = 0,
                smugmug_album_key = NULL,
                last_processed_timestamp = ?
        """
        params = [STATUS_PENDING, now_timestamp]
        
        try:
            with self.conn:
                cursor = self.conn.execute(sql, params)
            reset_count = cursor.rowcount
            logger.info(f"Reset {reset_count} items to PENDING, cleared album assignments but kept MD5 hashes.")
            return reset_count
        except sqlite3.Error as e:
            logger.error(f"Error resetting all items: {e}", exc_info=True)
            return 0
    
    def close(self):
        """Closes the database connection."""
        if self.conn:
            try:
                self.conn.close()
                logger.info("DB connection closed.")
                self.conn = None
            except sqlite3.Error as e:
                logger.error(f"Error closing DB connection: {e}", exc_info=True)

    def add_or_update_album(self, album_key, album_name, album_uri, folder_name=None, is_current=False):
        """Adds or updates an album in the albums tracking table."""
        if not self.conn:
            logger.error("Cannot add/update album: No database connection.")
            return False
        
        # Check if album already exists
        try:
            cursor = self.conn.execute(
                f"SELECT album_key, item_count FROM {ALBUMS_TABLE_NAME} WHERE album_key = ?",
                (album_key,)
            )
            existing_album = cursor.fetchone()
            
            if existing_album:
                # Update existing album
                sql = f"""
                    UPDATE {ALBUMS_TABLE_NAME}
                    SET album_name = ?, album_uri = ?, folder_name = ?, is_current = ?
                    WHERE album_key = ?
                """
                params = [album_name, album_uri, folder_name, 1 if is_current else 0, album_key]
                
                # If marking as current, ensure all others are not current
                if is_current:
                    with self.conn:
                        self.conn.execute(f"UPDATE {ALBUMS_TABLE_NAME} SET is_current = 0")
                        self.conn.execute(sql, params)
                else:
                    with self.conn:
                        self.conn.execute(sql, params)
                
                logger.info(f"Updated album in tracking table: {album_name} (Key: {album_key})")
                return True
            else:
                # Insert new album
                sql = f"""
                    INSERT INTO {ALBUMS_TABLE_NAME}
                    (album_key, album_name, album_uri, folder_name, is_current)
                    VALUES (?, ?, ?, ?, ?)
                """
                params = [album_key, album_name, album_uri, folder_name, 1 if is_current else 0]
                
                # If marking as current, ensure all others are not current
                if is_current:
                    with self.conn:
                        self.conn.execute(f"UPDATE {ALBUMS_TABLE_NAME} SET is_current = 0")
                        self.conn.execute(sql, params)
                else:
                    with self.conn:
                        self.conn.execute(sql, params)
                
                logger.info(f"Added new album to tracking table: {album_name} (Key: {album_key})")
                return True
        except sqlite3.Error as e:
            logger.error(f"Error adding/updating album {album_key}: {e}", exc_info=True)
            return False
    
    def get_all_albums(self):
        """Retrieves all albums from the tracking table."""
        if not self.conn:
            logger.error("Cannot get albums: No database connection.")
            return []
        
        try:
            cursor = self.conn.execute(f"SELECT * FROM {ALBUMS_TABLE_NAME} ORDER BY creation_timestamp")
            albums = [dict(row) for row in cursor.fetchall()]
            return albums
        except sqlite3.Error as e:
            logger.error(f"Error retrieving albums: {e}", exc_info=True)
            return []
    
    def get_current_album(self):
        """Retrieves the current album from the tracking table."""
        if not self.conn:
            logger.error("Cannot get current album: No database connection.")
            return None
        
        try:
            cursor = self.conn.execute(f"SELECT * FROM {ALBUMS_TABLE_NAME} WHERE is_current = 1")
            row = cursor.fetchone()
            if row:
                return dict(row)
            else:
                return None
        except sqlite3.Error as e:
            logger.error(f"Error retrieving current album: {e}", exc_info=True)
            return None
    
    def mark_album_as_current(self, album_key):
        """Marks an album as the current album and ensures all others are not current."""
        if not self.conn:
            logger.error("Cannot mark album as current: No database connection.")
            return False
        
        try:
            with self.conn:
                # First, set all albums to not current
                self.conn.execute(f"UPDATE {ALBUMS_TABLE_NAME} SET is_current = 0")
                
                # Then, set the specified album as current
                self.conn.execute(
                    f"UPDATE {ALBUMS_TABLE_NAME} SET is_current = 1 WHERE album_key = ?",
                    (album_key,)
                )
            
            logger.info(f"Marked album {album_key} as current")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error marking album {album_key} as current: {e}", exc_info=True)
            return False
    
    def increment_album_item_count(self, album_key):
        """Increments the item count for an album."""
        if not self.conn:
            logger.error("Cannot increment album item count: No database connection.")
            return False
        
        try:
            with self.conn:
                self.conn.execute(
                    f"UPDATE {ALBUMS_TABLE_NAME} SET item_count = item_count + 1 WHERE album_key = ?",
                    (album_key,)
                )
            
            logger.debug(f"Incremented item count for album {album_key}")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error incrementing item count for album {album_key}: {e}", exc_info=True)
            return False
    
    def get_album_item_count(self, album_key):
        """Gets the current item count for an album."""
        if not self.conn:
            logger.error("Cannot get album item count: No database connection.")
            return -1
        
        try:
            cursor = self.conn.execute(
                f"SELECT item_count FROM {ALBUMS_TABLE_NAME} WHERE album_key = ?",
                (album_key,)
            )
            row = cursor.fetchone()
            if row:
                return row[0]
            else:
                return 0
        except sqlite3.Error as e:
            logger.error(f"Error getting item count for album {album_key}: {e}", exc_info=True)
            return -1
    
    def is_album_near_capacity(self, album_key, capacity_threshold=0.8, max_capacity=5000):
        """
        Checks if an album is approaching the capacity threshold (default 80%).
        Returns True if the album is at or above the threshold, False otherwise.
        """
        if not self.conn:
            logger.error("Cannot check album capacity: No database connection.")
            return True  # Conservative approach: assume near capacity if can't check
        
        try:
            item_count = self.get_album_item_count(album_key)
            if item_count < 0:
                logger.error(f"Error getting item count for album {album_key}")
                return True  # Conservative approach
            
            threshold_count = int(max_capacity * capacity_threshold)
            is_near = item_count >= threshold_count
            
            if is_near:
                logger.warning(f"Album {album_key} is at {item_count}/{max_capacity} items "
                              f"(≥{capacity_threshold*100}% threshold of {threshold_count})")
            
            return is_near
        except Exception as e:
            logger.error(f"Error checking album capacity for {album_key}: {e}", exc_info=True)
            return True  # Conservative approach
    
    def __del__(self):
        """Ensure connection is closed when the object is garbage collected."""
        if self.conn:
            logger.warning("DBManager deleted without explicit close. Closing now.")
            self.close()
