import hashlib
import zlib
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np

from aiops.dedup.normalize import normalize_trace
from aiops.ingest.schemas import LogEvent

VECTOR_SIZE = 256


def text_to_vector(text: str) -> np.ndarray:
    """Turn text into a list of numbers (a vector) by counting 3-letter chunks.
    Similar texts produce similar vectors. No AI model needed."""
    text = text.lower()
    vector = np.zeros(VECTOR_SIZE, dtype=np.float64)
    for i in range(len(text) - 2):
        chunk = text[i:i + 3]
        vector[zlib.crc32(chunk.encode()) % VECTOR_SIZE] += 1.0
    norm = np.linalg.norm(vector)
    return vector / norm if norm > 0 else vector


def cosine_similarity(a: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Similarity (0 to 1) between one vector and many. Vectors are already length 1,
    so the dot product is the cosine similarity."""
    return matrix @ a


@dataclass
class Incident:
    id: int
    service_name: str
    error_class: str
    signature: str
    first_seen: str
    last_seen: str
    count: int
    sample_trace: str


class DedupEngine:
    def __init__(self, threshold: float = 0.92):
        self.threshold = threshold
        self.incidents: Dict[int, Incident] = {}
        self._by_signature: Dict[str, int] = {}
        # per service: list of (incident_id, vector) for near-duplicate search
        self._vectors: Dict[str, List[Tuple[int, np.ndarray]]] = {}
        self._next_id = 1

    def process(self, event: LogEvent) -> Tuple[Incident, bool]:
        """Returns (incident, is_new). is_new is False when this error was seen before."""
        normalized = normalize_trace(f"{event.error_class} {event.stack_trace}")
        signature = hashlib.sha256(
            f"{event.service_name}|{normalized}".encode()
        ).hexdigest()

        # 1) Exact match: same normalized error seen before
        if signature in self._by_signature:
            return self._bump(self._by_signature[signature], event), False

        # 2) Near match: very similar error from the same service
        vector = text_to_vector(normalized)
        candidates = self._vectors.get(event.service_name, [])
        if candidates:
            matrix = np.vstack([v for _, v in candidates])
            scores = cosine_similarity(vector, matrix)
            best = int(np.argmax(scores))
            if scores[best] >= self.threshold:
                incident_id = candidates[best][0]
                self._by_signature[signature] = incident_id
                return self._bump(incident_id, event), False

        # 3) Brand new error
        incident = Incident(
            id=self._next_id,
            service_name=event.service_name,
            error_class=event.error_class,
            signature=signature,
            first_seen=event.timestamp,
            last_seen=event.timestamp,
            count=1,
            sample_trace=event.stack_trace,
        )
        self._next_id += 1
        self.incidents[incident.id] = incident
        self._by_signature[signature] = incident.id
        self._vectors.setdefault(event.service_name, []).append((incident.id, vector))
        return incident, True

    def _bump(self, incident_id: int, event: LogEvent) -> Incident:
        incident = self.incidents[incident_id]
        incident.count += 1
        incident.last_seen = event.timestamp
        return incident