import pytest

from aiops.rag.store import RunbookStore, chunk_runbook, parse_runbook


@pytest.fixture()
def store(tmp_path):
    s = RunbookStore(path=str(tmp_path / "chroma"))
    s.load_directory("runbooks/default")
    return s


def test_parse_runbook_reads_settings():
    meta, body = parse_runbook("---\nservice_name: a\nerror_type: KeyError\n---\n# Title\ntext")
    assert meta == {"service_name": "a", "error_type": "KeyError"}
    assert body.startswith("# Title")


def test_long_runbooks_are_split_by_heading():
    body = "# T\n## One\n" + "x" * 800 + "\n## Two\n" + "y" * 800
    assert len(chunk_runbook(body)) == 2
    assert len(chunk_runbook("# short")) == 1


def test_loading_is_repeatable(store):
    first = store.count()
    store.load_directory("runbooks/default")
    assert store.count() == first  # no duplicates after loading again


@pytest.mark.parametrize("service, signature, expected", [
    ("payment-service", "KeyError: 'STRIPE_API_KEY'", "missing_env_var.md"),
    ("order-service", "TimeoutError: connection pool timeout", "db_connection_pool_timeout.md"),
    ("user-service", "AttributeError: 'NoneType' object has no attribute 'email'", "null_user_lookup.md"),
])
def test_best_runbook_is_the_right_one(store, service, signature, expected):
    hits = store.get_relevant_context(service, signature, top_k=2)
    assert hits[0]["source"] == expected


def test_other_services_runbooks_are_not_returned(store):
    hits = store.get_relevant_context("user-service", "KeyError: 'STRIPE_API_KEY'", top_k=6)
    assert all(h["service_name"] in ("user-service", "*") for h in hits)


def test_generic_runbook_applies_to_any_service(store):
    hits = store.get_relevant_context("inventory-service", "MemoryError", top_k=2)
    assert hits[0]["source"] == "out_of_memory.md"


def test_each_runbook_appears_once(store):
    hits = store.get_relevant_context("order-service", "TimeoutError", top_k=5)
    sources = [h["source"] for h in hits]
    assert len(sources) == len(set(sources))


def test_empty_store_returns_nothing(tmp_path):
    empty = RunbookStore(path=str(tmp_path / "empty"))
    assert empty.get_relevant_context("x", "y") == []


def test_tenants_are_separate(tmp_path):
    a = RunbookStore(path=str(tmp_path / "c"), tenant_id="acme")
    b = RunbookStore(path=str(tmp_path / "c"), tenant_id="globex")
    a.load_directory("runbooks/default")
    assert b.get_relevant_context("payment-service", "KeyError") == []


def test_parse_runbook_removes_quotes_around_values():
    meta, _ = parse_runbook('---\nservice_name: "*"\n---\nbody')
    assert meta["service_name"] == "*"
