import hashlib
from pathlib import Path


def fingerprint(snapshot: Path) -> str:
    """Hash schema/sequence statements and COPY rows as a multiset per table."""
    digest = hashlib.sha256()
    with snapshot.open("rb") as stream:
        for line in stream:
            digest.update(line)
            if line.startswith(b"COPY ") and line.rstrip(b"\r\n").endswith(b" FROM stdin;"):
                rows = []
                for line in stream:
                    if line.rstrip(b"\r\n") == b"\\.":
                        break
                    rows.append(line)
                for row in sorted(rows):
                    digest.update(row)
                digest.update(line)
    return digest.hexdigest()


class Cache:
    def __init__(self) -> None:
        self.results: dict[str, bool] = {}
        self.lookups = 0
        self.hits = 0
        self.accepted_hits = 0
        self.rejected_hits = 0

    def get(self, key: str) -> bool | None:
        self.lookups += 1
        result = self.results.get(key)
        if result is not None:
            self.hits += 1
            if result:
                self.accepted_hits += 1
            else:
                self.rejected_hits += 1
        return result
