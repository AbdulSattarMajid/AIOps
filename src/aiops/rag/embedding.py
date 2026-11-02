import re
import zlib

import numpy as np

DIMENSIONS = 512
_WORD = re.compile(r"[a-z0-9]+")


def embed_text(text: str) -> list[float]:
    """Turn text into a list of numbers so similar texts get similar lists.
    Counts words (and parts of snake_case words) plus 3-letter chunks.
    It needs no download and works offline. A real AI embedding model can replace
    this function later without changing anything else."""
    text = text.lower()
    vector = np.zeros(DIMENSIONS, dtype=np.float64)

    for word in _WORD.findall(text):
        vector[zlib.crc32(word.encode()) % DIMENSIONS] += 1.0
    for chunk in (text[i:i + 3] for i in range(len(text) - 2)):
        vector[zlib.crc32(("c:" + chunk).encode()) % DIMENSIONS] += 0.25

    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector.tolist()
