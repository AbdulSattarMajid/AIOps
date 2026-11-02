from typing import Optional

from pydantic import BaseModel


class LogEvent(BaseModel):
    """One error log sent by a microservice."""

    timestamp: str
    service_name: str
    error_class: str
    stack_trace: str
    message: Optional[str] = None