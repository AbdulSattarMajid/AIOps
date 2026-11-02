from pathlib import Path

import chromadb

from aiops.rag.embedding import embed_text

DEFAULT_TENANT = "default"
GENERIC = "*"  # a runbook for "*" applies to every service
LONG_RUNBOOK_CHARS = 1200


def parse_runbook(text: str) -> tuple[dict[str, str], str]:
    """Split a runbook file into (settings, body). Settings sit between two '---' lines."""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].strip().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    meta[key.strip()] = value.strip().strip('"\'')
            body = parts[2].strip()
    return meta, body


def chunk_runbook(body: str) -> list[str]:
    """Short runbooks stay in one piece. Long ones are split at '## ' headings."""
    if len(body) <= LONG_RUNBOOK_CHARS:
        return [body]
    title, _, rest = body.partition("\n## ")
    sections = ["## " + s for s in rest.split("\n## ")] if rest else []
    return [f"{title}\n{s}" for s in sections] or [body]


class RunbookStore:
    def __init__(self, path: str = "chroma_data", tenant_id: str = DEFAULT_TENANT):
        self.tenant_id = tenant_id
        self._client = chromadb.PersistentClient(path=path)
        # One collection per tenant, so one customer's runbooks never mix with another's.
        self._collection = self._client.get_or_create_collection(f"runbooks_{tenant_id}")

    def load_directory(self, directory: str) -> int:
        """Read every .md runbook in `directory` and store it. Safe to run repeatedly."""
        ids, documents, metadatas, embeddings = [], [], [], []
        for file in sorted(Path(directory).glob("*.md")):
            meta, body = parse_runbook(file.read_text(encoding="utf-8"))
            title = meta.get("title") or file.stem.replace("_", " ")
            for i, chunk in enumerate(chunk_runbook(body)):
                # Title and error type are repeated so they count more than the body text.
                head = f"{title}\n{meta.get('error_type', '')}\n"
                text = head * 3 + chunk
                ids.append(f"{self.tenant_id}:{file.stem}:{i}")
                documents.append(chunk)
                metadatas.append({
                    "tenant_id": self.tenant_id,
                    "service_name": meta.get("service_name", GENERIC),
                    "error_type": meta.get("error_type", "unknown"),
                    "severity": meta.get("severity", "medium"),
                    "title": title,
                    "source": file.name,
                })
                embeddings.append(embed_text(text))
        if ids:
            self._collection.upsert(ids=ids, documents=documents,
                                    metadatas=metadatas, embeddings=embeddings)
        return len(ids)

    def count(self) -> int:
        return self._collection.count()

    def get_relevant_context(self, service_name: str, error_signature: str,
                             top_k: int = 2) -> list[dict]:
        """Best runbooks for this error: written for this service, or generic ones."""
        total = self._collection.count()
        if total == 0:
            return []
        result = self._collection.query(
            query_embeddings=[embed_text(error_signature)],  # service is handled by the filter below
            n_results=min(total, top_k * 3),
            where={"$or": [{"service_name": service_name}, {"service_name": GENERIC}]},
        )
        best: dict[str, dict] = {}
        for doc, meta, distance in zip(result["documents"][0], result["metadatas"][0],
                                       result["distances"][0]):
            if meta["source"] in best:
                continue  # keep only the best chunk of each runbook
            best[meta["source"]] = {
                "title": meta["title"], "source": meta["source"],
                "service_name": meta["service_name"], "error_type": meta["error_type"],
                "severity": meta["severity"], "distance": round(float(distance), 3),
                "text": doc,
            }
        return list(best.values())[:top_k]


_default_store: RunbookStore | None = None


def get_relevant_context(service_name: str, error_signature: str, top_k: int = 2) -> list[dict]:
    """Convenience wrapper that uses the default store in ./chroma_data."""
    global _default_store
    if _default_store is None:
        _default_store = RunbookStore()
    return _default_store.get_relevant_context(service_name, error_signature, top_k)
