"""Importing from any PhotoSource into any PhotoDestination: what is already there, and what to upload.

  inventory.py  snapshot the destination albums in scope (dest_albums, dest_items)
  dedupe.py     decide, per source item, exact | same | new | review (pure rules + cached hashing)
"""
