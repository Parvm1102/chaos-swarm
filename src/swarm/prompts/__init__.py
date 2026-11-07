"""Prompt files: plain text, loaded by name, versioned by content.

Every prompt text the model sees lives in this folder as `<name>.txt`. `load_prompt(name)`
returns the text exactly as stored and its `version`, the sha256 of the file's bytes. Every
record stores that version as `prompt_version`, so two runs used the same prompt exactly when
their versions match.

Templates use `string.Template` placeholders ($peer, $round, ...). `render` fills them and
fails if any placeholder is left without a value.
"""

import hashlib
import re
import string
from dataclasses import dataclass
from importlib import resources

_NAME = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str  # sha256 hex digest of the file's bytes


def load_prompt(name: str) -> Prompt:
    """The prompt stored in `<name>.txt`. Raises FileNotFoundError if there is none."""
    if not _NAME.match(name):
        raise ValueError(f"prompt names are lower-case words joined by underscores, got {name!r}")
    data = (resources.files(__package__) / f"{name}.txt").read_bytes()
    return Prompt(name=name, text=data.decode("utf-8"), version=hashlib.sha256(data).hexdigest())


def render(prompt: Prompt, **values: object) -> str:
    """The template with every placeholder filled, without surrounding blank lines.

    Raises KeyError if a placeholder has no value (a harness bug, never a model's).
    """
    return string.Template(prompt.text).substitute(values).strip()
