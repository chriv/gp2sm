# Troubleshooting

**"no gp2sm project here"**
Run commands inside a project folder (one with `gp2sm.toml`) or pass `--project DIR`. Create one with `gp2sm init`.

**"no stored credentials 'smugmug'"**
Run `gp2sm auth smugmug` (or `gp2sm auth smugmug --name NAME` for the name your project's `credentials` setting uses). `gp2sm status` shows what the project expects.

**"another gp2sm command is running on this project"**
Only one command at a time may change a project. Wait for the other one. If you're sure nothing is running (for example after a crash on another machine sharing the folder), delete the `.gp2sm.lock` file the message names.

**"… was made by a pre-release gp2sm and can't be used"**
That state database comes from a development version with a different layout. Start a new project with `gp2sm init`. Your photos on SmugMug are unaffected.

**Uploads fail with "unknown file type" or "video too small"**
SmugMug refuses some files outright: WebP/BMP/ICO images and low-resolution videos. They're listed in the report as failed uploads. Converting the images (`rejected_types = "convert"`) avoids the first case; the videos can't be uploaded.

**A run was interrupted, or the network dropped**
Run the same command again. Anything whose outcome is unknown is checked against SmugMug first, so nothing is uploaded or moved twice.

**Items are "held for review"**
The checks couldn't decide on their own (two pictures are similar, but not clearly the same). Run `gp2sm takeout review`, sort the pairs in the project's `review/` folder into `same/` or `different/`, then `gp2sm takeout review --read` and `gp2sm plan`. Album renames and setting fixes for review are approved with `gp2sm albums approve --name "ALBUM"`.

**The report says the counts don't add up**
That's a bug: every file should be counted exactly once. Please open an issue with the report's `report.json` (it names your files, so check it before sharing).

**Where are the logs?**
In the project's `logs/gp2sm.log`, with full detail of every request.
