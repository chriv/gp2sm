"""Google Takeout: indexing, pairing, the PhotoSource, and upload planning."""


def service():
    from gp2sm.services.registry import ServiceInfo
    from gp2sm.takeout.source import TakeoutSource
    return ServiceInfo(name="google-takeout", kind="source",
                       factory=lambda index_db, takeout_dir: TakeoutSource(index_db, takeout_dir),
                       description="Google Takeout archives (.zip or .tgz) of Google Photos")
