"""The server's own reading of a spoken confirmation. Deliberately stricter than the browser's: the whole transcript
must be one of a few fixed affirmative phrases. Anything else ("sounds good", "that's fine", "yes but", an empty or
partial transcript) is not a confirmation. The browser never decides on its own that the user said yes."""

from __future__ import annotations

import re
from typing import Literal

AFFIRMATIVE = frozenset({
    "yes", "yes please", "yes proceed", "proceed", "yes go ahead", "go ahead", "confirm", "confirmed", "i confirm",
    "yes i confirm", "yes do it", "do it", "affirmative", "yes tag them", "yes tag it",
})
NEGATIVE = frozenset({"no", "nope", "no thanks", "cancel", "stop", "don't", "do not", "never mind", "abort", "no stop"})
# A separate, distinct acknowledgement for overwriting existing values: a plain "yes" never counts.
OVERWRITE_ACK = frozenset({
    "i confirm the overwrite", "i confirm the overwrites", "confirm the overwrite", "confirm the overwrites",
    "confirm overwrite", "confirm overwrites", "yes overwrite them", "yes overwrite it", "overwrite them",
    "overwrite it", "yes i confirm the overwrite", "yes i confirm the overwrites",
})


def normalize(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[.,!?;:\"()]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return {"yeah": "yes", "yep": "yes", "okay proceed": "proceed"}.get(t, t)


def read_confirmation(transcript: str | None) -> Literal["yes", "no", "unclear"]:
    if not isinstance(transcript, str) or len(transcript) > 200:
        return "unclear"
    t = normalize(transcript)
    if t in AFFIRMATIVE:
        return "yes"
    if t in NEGATIVE or t.startswith(("no ", "cancel ", "stop ")):
        return "no"
    return "unclear"


def is_overwrite_ack(transcript: str | None) -> bool:
    return isinstance(transcript, str) and len(transcript) <= 200 and normalize(transcript) in OVERWRITE_ACK
