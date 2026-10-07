"""Unclear "is it already there?" cases, for a person to decide by dragging files.

`export` writes one flat folder:

  review/
    README.txt
    0001 source IMG_1234.jpg          <- the item to import (converted to JPEG if needed, so it opens anywhere)
    0001 destination IMG_1234.jpg     <- the same-named copy already on the destination
    same/                             <- drag both files of a pair here if they are the same photo
    different/                        <- ... or here if they are different photos

`read_answers` reads where the files ended up (either file of a pair is enough) and records the answer in
source_matches.reviewed; `gp2sm takeout plan` then uploads the different ones and skips the same ones.
"""

import json
import os
import re

from gp2sm.media.convert import HEIF_EXTS, to_jpeg
from gp2sm.state import now

README = """Each number is one photo from the import and a same-named copy already on the destination.
Look at each pair, then drag its files into:
  same/        the same photo (it won't be uploaded again)
  different/   different photos (the import's photo will be uploaded)
Pairs you leave here stay on hold. When you're done, run:  gp2sm takeout review --read
"""
PAIR = re.compile(r"^(\d{4,}) (source|destination) ")


def pending(st):
    return [dict(r) for r in st.q("SELECT source_ref, dest_item_id FROM source_matches "
                                  "WHERE decision='review' AND reviewed IS NULL ORDER BY source_ref")]


def export(st, client, folder, read_refs):
    rows = pending(st)
    os.makedirs(os.path.join(folder, "same"), exist_ok=True)
    os.makedirs(os.path.join(folder, "different"), exist_ok=True)
    with open(os.path.join(folder, "README.txt"), "w") as f:
        f.write(README)
    index_path = os.path.join(folder, "index.json")
    index = json.load(open(index_path)) if os.path.exists(index_path) else {}
    numbers = {ref: n for n, ref in index.items()}
    names = dict(st.q("SELECT item_id, name FROM dest_items"))
    written = 0
    by_ref = {r["source_ref"]: r for r in rows}
    for ref, data in read_refs([r["source_ref"] for r in rows]):
        n = numbers.get(ref) or f"{len(index) + 1:04d}"
        index[n], numbers[ref] = ref, n
        base = os.path.basename(ref.split("::", 1)[-1])
        stem, ext = os.path.splitext(base)
        if ext.lower() in HEIF_EXTS:
            to_jpeg(data, ext.lower(), os.path.join(folder, f"{n} source {stem}.jpg"))
        else:
            with open(os.path.join(folder, f"{n} source {base}"), "wb") as f:
                f.write(data)
        dest_id = by_ref[ref]["dest_item_id"]
        if dest_id:
            dest_name = names.get(dest_id) or f"{dest_id}.jpg"
            with open(os.path.join(folder, f"{n} destination {dest_name}"), "wb") as f:
                f.write(client.preview_bytes(dest_id))
        written += 1
    with open(index_path, "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    return {"pairs_to_review": written, "folder": folder}


def read_answers(st, folder):
    index_path = os.path.join(folder, "index.json")
    if not os.path.exists(index_path):
        raise SystemExit(f"nothing exported to review in {folder}: run `gp2sm takeout review` first")
    index = json.load(open(index_path))
    answers, conflicts = {}, set()
    for answer in ("same", "different"):
        sub = os.path.join(folder, answer)
        for name in os.listdir(sub) if os.path.isdir(sub) else []:
            m = PAIR.match(name)
            if not m or m.group(1) not in index:
                continue
            n = m.group(1)
            if answers.get(n, answer) != answer:
                conflicts.add(n)
            answers[n] = answer
    out = {"same": 0, "different": 0, "conflicting": sorted(conflicts)}
    with st.db:
        for n, answer in answers.items():
            if n in conflicts:
                continue
            st.db.execute("UPDATE source_matches SET reviewed=?, decided_at=? WHERE source_ref=?",
                          (answer, now(), index[n]))
            st.event("review_answer", commit=False, source_ref=index[n], answer=answer)
            out[answer] += 1
    return out
