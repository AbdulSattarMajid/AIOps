import importlib

from aiops.worker import worker


def test_worker_package_entrypoint_import_does_not_start_worker(monkeypatch):
    def unexpected_start():
        raise AssertionError("import must not start the worker loop")

    monkeypatch.setattr(worker, "run_worker", unexpected_start)
    entrypoint = importlib.import_module("aiops.worker.__main__")
    entrypoint = importlib.reload(entrypoint)
    assert entrypoint.run_worker is unexpected_start
