import hashlib
import zlib
from dataclasses import dataclass, field
from typing import Dict, Tuple

import numpy as np

from aiops.dedup.normalize import normalize_trace
from aiops.ingest.schemas import LogEvent
from aiops.security.redaction import redact_log_event
from aiops.storage.redis_store import DedupStore, RedisStore

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
    redaction: Dict[str, int] = field(default_factory=dict)


class DedupEngine:
    def __init__(
        self, threshold: float = 0.92, store: DedupStore | None = None
    ):
        self.threshold = threshold
        self.store = store if store is not None else RedisStore()

    @property
    def incidents(self) -> Dict[int, Incident]:
        return {
            incident["id"]: Incident(**incident)
            for incident in self.store.list_incidents()
        }

    def process(self, event: LogEvent) -> Tuple[Incident, bool]:
        """Returns (incident, is_new). is_new is False when this error was seen before."""
        event, findings = redact_log_event(event)
        normalized = normalize_trace(f"{event.error_class} {event.stack_trace}")
        signature = hashlib.sha256(
            f"{event.service_name}|{normalized}".encode()
        ).hexdigest()
        vector = text_to_vector(normalized)
        stored, is_new = self.store.process(
            {
                "service_name": event.service_name,
                "error_class": event.error_class,
                "timestamp": event.timestamp,
                "sample_trace": event.stack_trace,
                "redaction": dict(findings),
            },
            signature,
            vector.tolist(),
            self.threshold,
        )
        return Incident(**stored), is_new

    def get_incident(self, incident_id: int):
        stored = self.store.get_incident(incident_id)
        return Incident(**stored) if stored is not None else None