## AI Coding Constraints for gp2sm Project

This document outlines specific constraints and coding standards for the AI (Gemini) to follow when generating or modifying code for the Google Photos to SmugMug (gp2sm) project. The goal is to improve code quality, maintainability, and reduce debugging cycles.

**General:**

1.  **Version Numbering:** Do **not** increment the `__version__` variable in any file unless explicitly requested by the user.
2.  **TODO Lists:** Do **not** add, remove, or modify any TODO comments or lists within the code. These are maintained by the user.

**Core Formatting & Style:**

3.  **One Statement Per Line:** Strictly adhere to placing only one Python statement per line. Do **not** use semicolons (`;`) to combine multiple statements on a single line.
    * *Incorrect:* `x = 1; y = 2`
    * *Correct:*
        ```python
        x = 1
        y = 2
        ```
4.  **Expand Control Flow Blocks:** Ensure that control flow blocks (`if`, `for`, `while`, `with`, `try`/`except`/`finally`) always have their bodies on separate, indented lines. Do **not** collapse the block header and its body onto a single line, even for single-line bodies.
    * *Incorrect:* `if condition: do_something()`
    * *Incorrect:* `with open(f) as file: process(file)`
    * *Correct:*
        ```python
        if condition:
            do_something()

        with open(f) as file:
            process(file)
        ```
5.  **PEP 8 Compliance:** Follow standard Python PEP 8 style guidelines regarding indentation (4 spaces), line length (aim for under 100 characters where feasible, break long lines appropriately), naming conventions, whitespace, etc.
6.  **Clear Comments:** Provide thorough comments explaining the purpose of functions, complex logic blocks, and potentially non-obvious code sections.
7.  **Attribution Comments:** Keep existing attribution comments (e.g., `# Attribution: ...`) at the top of files or in relevant sections. Do not remove them.
8.  **Logging Over Print:** Use the configured `logger` instance (from the `logging` module) for all informational, debug, warning, error, and critical messages. Avoid using the built-in `print()` function for standard output, except where explicitly necessary for user interaction outside the logging system (e.g., the OAuth verifier input prompt).
9.  **Console Logger Configuration:** Ensure the console logger (`colorlog.StreamHandler`) is configured to use `sys.stdout`. Do not change the existing color formatting setup (`colorlog.ColoredFormatter`).

**Functionality & Reliability:**

10. **Imports:** Do **not** wrap `import` statements in `try...except` blocks to suppress `ImportError`. Missing dependencies should cause the script to fail immediately upon starting, rather than potentially failing later at runtime.
11. **Modularity:** Keep site-specific code contained within the appropriate module (`google_photos_module.py` for Google Photos API interactions, `smugmug_module.py` for SmugMug API interactions). Avoid placing detailed API call logic, site-specific data parsing, or endpoint construction directly within `main.py` whenever possible; prefer calling methods from the respective modules. `main.py` should focus on orchestration, configuration, and the core processing loop.
12. **SmugMug API Endpoint Usage:**
    * Prefer using specific object endpoints (e.g., `!children`, `!images`) over generic `!search` endpoints when checking for the existence of folders, albums, or images, especially when needing reliable `Uri` or `Type` information in the response.
    * Acknowledge that `!search` results may lack certain fields (like `Type` or `Uri`) and implement checks accordingly or use more reliable endpoints.
13. **Error Handling:**
    * Implement robust error handling for API calls (both Google Photos and SmugMug), including specific checks for common HTTP errors (400, 401, 403, 404, 409, 429, 5xx).
    * Handle potential `None` return values or missing keys in API responses gracefully using `.get()` or `try...except` blocks.
    * **Google Photos Quota (429):** Detect 429 errors. Do *not* implement exponential backoff. Instead, set the `quota_exceeded_flag`, log a critical error, and ensure the script initiates a graceful shutdown, reporting the quota issue clearly in the final summary.
    * **SmugMug Album Full (Code 63):** Detect error code 63. Implement the "live switch" logic: update the database config snapshot *and* the shared `smugmug` object instance to target the next sequential album without requiring a script restart. Ensure the logic correctly identifies the *next* album name based on the one that just filled up.
14. **Progress Reporting:** Ensure the main progress log message (`logger.progress`) displays **overall cumulative totals** for *all* stats shown (Uploaded, Duplicates, Skipped, Errors), reflecting the sum of items already in those states in the database *before* the current run plus the items processed *during* the current run. The percentage completion should also be based on the total items in the database. Run-specific counters can be maintained internally for the final summary but should not be the primary display in the live progress line.
15. **State Management:** Rely on the database (`run_config` table) as the source of truth for the target SmugMug album between runs, especially after an album switch. Handle potential mismatches between the config file/arguments and the stored state gracefully, prioritizing the stored state for resuming runs.
16. **File Handling:** Ensure temporary downloaded files are consistently cleaned up, especially in error paths or `finally` blocks. Check for the existence of temp files before attempting operations like hashing or uploading.
17. **Concurrency:** Ensure thread-safe coding practices are applied to sections of code that are executed in parallel threads.

**Interaction & Debugging:**

18. **Regression Prevention:** Actively try to avoid reintroducing previously fixed bugs or deviating from established project conventions (like the progress reporting format). Refer back to these constraints and recent conversation history when generating updates.
