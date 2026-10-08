# Safety: dry runs, verification and undo

gp2sm assumes things go wrong: a laptop sleeps, the network drops, SmugMug times out but still does the work. These are the rules every command follows.

## Nothing changes SmugMug without `--yes`

Commands that change SmugMug (`apply`, `takeout upload`, `organize apply`, `albums apply`, `albums fix`, every `undo`, every `delete-*`) only show what they would do unless you add `--yes`. Planning, reporting, reviewing and verifying never change SmugMug.

## Everything is recorded, before it happens

Each project has a state database (`state.db`). An upload or move is recorded as "in progress" *before* it's sent, and as done only once SmugMug confirms it. If SmugMug's answer is lost (a timeout, a server error), gp2sm doesn't retry blindly: it asks SmugMug where the item is now, and goes on from there. Every action is also appended to an event log in the same database.

## Stopping and resuming

Press Ctrl-C once and the command finishes the work in flight, then stops. Press it twice to stop at once. Either way, run the same command again to continue. Only one command at a time may change a project; a second one is refused while the first runs, and a lock left by a crashed run is detected and taken over.

## Checking

`gp2sm verify` compares SmugMug with what the project recorded: every upload, every moved item, every renamed album and changed setting. Run it after `apply`. `gp2sm report` accounts for every source file and states whether everything has been verified.

Checks exit with code 3 when they find something to look at (`verify`: a mismatch or a name or setting changed since; `albums audit`: drift from a policy; `plan`: work to do), and 0 when everything is in order, so a script or a scheduler can tell the two apart. See the FAQ.

## What can be undone

| what | undo |
|---|---|
| organize: items moved into an album | `gp2sm organize undo "ALBUM" --yes` moves them back to where they came from |
| organize: items collected (`mode = "collect"`) | the same command removes only the collected copies; the originals were never moved |
| album renames | `gp2sm albums undo --yes` restores the old names (only the display name ever changes, so links keep working) |
| album settings | `gp2sm albums undo --yes` restores the previous values (except values SmugMug itself set that can't be written back; those are held for review instead of changed) |
| Takeout uploads | `gp2sm takeout remove UPLOAD-ID ... --yes` deletes specific uploads gp2sm made |

Undo leaves alone anything that was changed again after gp2sm changed it, and says so.

## What can't be undone

Deletions are permanent, so they're separate commands, dry runs by default, and checked against SmugMug first:
- `gp2sm organize delete-duplicates --yes` deletes the album of parked identical copies, only after confirming that it holds exactly the planned copies and that the kept copy of each is in place, and that no copy is also in another album (on SmugMug, deleting a photo removes it from every album it was collected into).
- `gp2sm organize delete-empty-sources --yes` deletes source albums SmugMug reports as empty. It refuses to run when anything was collected, because deleting an original on SmugMug deletes every collected copy of it too.
- `gp2sm organize delete-empty-targets --yes` deletes empty albums gp2sm itself created that nothing is planned for.
