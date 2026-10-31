"""Consistent read-only capture snapshots for research and offline emulation."""
import json
import sqlite3
from collections import OrderedDict
from pathlib import Path

import zstandard


class Archive:
    def __init__(self, path):
        source = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            source.backup(self.db)
        finally:
            source.close()
        self.db.row_factory = sqlite3.Row
        self.cache = OrderedDict()

    def events(self):
        refs = {}
        for r in self.db.execute("SELECT ep.event_id,ep.role,ep.hash,p.size FROM event_payloads ep JOIN payloads p ON p.hash=ep.hash"):
            refs.setdefault(r["event_id"], {})[r["role"]] = {"hash": r["hash"], "size": r["size"]}
        for r in self.db.execute("SELECT id,detail FROM events ORDER BY ts,rowid"):
            event = json.loads(r["detail"])
            event["payloads"] = refs.get(r["id"], {})
            yield event

    def payload(self, digest):
        r = self.db.execute("SELECT p.offset,p.size,b.id,b.data,b.raw_size FROM payloads p JOIN blocks b ON b.id=p.block_id WHERE p.hash=?", (digest,)).fetchone()
        if r is None:
            raise KeyError(digest)
        if r["id"] not in self.cache:
            self.cache[r["id"]] = zstandard.ZstdDecompressor().decompress(r["data"], max_output_size=max(1, r["raw_size"]))
            while len(self.cache) > 4:
                self.cache.popitem(last=False)
        return self.cache[r["id"]][r["offset"]:r["offset"] + r["size"]]

    def close(self):
        self.db.close()
