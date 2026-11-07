"""Which pre-registration a finished arm ran under, checked against the file on disk.

PREREG.md grows by appending a section per experiment, each after SEPARATOR. An arm records the
sha256 of the whole file as it was at the arm's first launch. matches() accepts the file on disk
when it hashes to that value, or when the part before one of the later separators does: an
earlier experiment's text was left byte-identical and new sections were only appended.
"""

import hashlib
from pathlib import Path

SEPARATOR = "\n---\n\n# Pre-registration:"


def matches(path: Path, recorded_sha256: str) -> bool:
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() == recorded_sha256:
        return True
    text = data.decode("utf-8")
    index = text.find(SEPARATOR)
    while index != -1:
        if hashlib.sha256(text[:index].encode("utf-8")).hexdigest() == recorded_sha256:
            return True
        index = text.find(SEPARATOR, index + 1)
    return False
