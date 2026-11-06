# aiops_sdk

Install the SDK from your application and register it with FastAPI:

```python
from aiops_sdk import install

install(app, api_url="http://127.0.0.1:8000", api_key="...", service_name="shop-service")
```

Unhandled exceptions are re-raised after a best-effort background report. The
SDK also exposes `report_exception(exc)` for manual reporting after `install`.
Reports use `POST /api/v1/telemetry/logs` with `X-API-Key`; only the event
fields required by the AIOps log schema are sent. Delivery is best-effort,
limited to 10 reports per second, and network operations use a 2-second timeout.
