from typing import Optional

from pydantic import BaseModel, ConfigDict


class LogEvent(BaseModel):
    """One error log sent by a microservice."""

    model_config = ConfigDict(extra="allow")

    timestamp: str
    service_name: str
    error_class: str
    stack_trace: str
    message: Optional[str] = None