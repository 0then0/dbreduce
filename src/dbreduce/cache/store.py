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
        self.hits = 0

    def get(self, key: str) -> bool | None:
        result = self.results.get(key)
        if result is not None:
            self.hits += 1
        return result
