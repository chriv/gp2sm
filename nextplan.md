# Plan: Error Handling Improvements and Logic Refinement

## Goals

1. Prevent ephemeral errors (e.g., 429 from Google Photos, SmugMug album full) from being committed as final item statuses in the database.
2. Ensure permanent errors (e.g., unsupported file type, video too long) are properly recorded.
3. Refactor logic flow for better traceability and internal consistency, including resolving variable shadowing and untracked upload skips.
4. Ensure consistency and recovery potential from exceptions and partial errors.
5. Maintain full compliance with `constraints.md` including attribution, logging, and traceability standards.

---

## Tasks

### Task 1: Track Ephemeral vs. Permanent Errors

- Introduce a new boolean flag in `process_item_worker`: `ephemeral_error_occurred`.
- When encountering known ephemeral errors:
  - Set this flag.
  - Do **not** call `db_manager.update_item_status`.
  - Exit with `return google_id, final_status` (where `final_status` reflects the **prior state**).

Example:
    
    if quota_exceeded_flag.is_set():
        ephemeral_error_occurred = True
        return google_id, final_status

---

### Task 2: Clarify Final Status Tracking

- Avoid unnecessary shadowing of `final_status`.
- Introduce distinct, purpose-driven variables:
  - `db_status_to_set`
  - `status_reason_log`
- Only update the database if:
  - `ephemeral_error_occurred` is `False`, **and**
  - `db_status_to_set` is not `None`.

---

### Task 3: Ensure Cleanup Paths Don’t Mask Failures

- Ensure that early exits (e.g., due to shutdown or failed temp file cleanup) don’t silently pass as success.
- Log any aborted flow, and skip DB status updates for ephemeral shutdowns.
- Introduce flags such as:
  - `upload_attempted`
  - `upload_skipped_due_to_shutdown`

---

### Task 4: Handle Permanent Errors Clearly

Clearly define and mark permanent, unrecoverable errors:
- Unsupported MIME type.
- Video too long and rejected by SmugMug.
- Google Photos API returning an unrecoverable error (not quota-related).

Actions:
    
    db_manager.update_item_status(google_id, STATUS_ERROR_UNSUPPORTED_TYPE, "Rejected file type")

These statuses should be committed to the database and logged as **non-retryable**.

---

### Task 5: Normalize Exception Handling

- In the general `except Exception` block:
  - Default to `ephemeral_error_occurred = True` unless the error is clearly a permanent issue.
  - Return without a DB update for external faults like API downtime or quota-related issues.
  - Use `logger.warning` for recoverable service exceptions, and reserve `logger.critical` for internal logic faults or database failures.

---

### Task 6: Consolidate Return Logic

- Standardize return flow near the end of `process_item_worker`:

    if not ephemeral_error_occurred and db_status_to_set:
        db_manager.update_item_status(...)

    return google_id, final_status

This unifies decision logic and makes final behavior more predictable and debuggable.

---

## Constraints

All changes must:

- Respect `constraints.md` — including proper logging, attribution, and error traceability.
- Avoid external side effects unless explicitly logged and reversible.
- Keep database integrity intact.
- Distinguish between retryable (ephemeral) and permanent failure cases.
- Maintain backward compatibility with existing config and runtime behavior.
