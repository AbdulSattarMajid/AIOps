from __future__ import annotations

import os
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, model_validator

# Default size limits for LogEvent fields
MAX_SERVICE_NAME_LENGTH = int(os.getenv("MAX_SERVICE_NAME_LENGTH", "128"))
MAX_MESSAGE_LENGTH = int(os.getenv("MAX_MESSAGE_LENGTH", "10000"))
MAX_STACK_TRACE_LENGTH = int(os.getenv("MAX_STACK_TRACE_LENGTH", "100000"))
MAX_EXTRA_FIELDS = int(os.getenv("MAX_EXTRA_FIELDS", "20"))
MAX_EXTRA_KEY_LENGTH = int(os.getenv("MAX_EXTRA_KEY_LENGTH", "64"))
MAX_EXTRA_VALUE_LENGTH = int(os.getenv("MAX_EXTRA_VALUE_LENGTH", "4096"))
MAX_TOTAL_EXTRA_SIZE = int(os.getenv("MAX_TOTAL_EXTRA_SIZE", "32768"))

_CORE_FIELDS = {"timestamp", "service_name", "error_class", "stack_trace", "message"}


class LogEvent(BaseModel):
    """One error log sent by a microservice with strict size caps."""

    model_config = ConfigDict(extra="allow")

    timestamp: str
    service_name: str
    error_class: str
    stack_trace: str
    message: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def validate_size_caps(cls, data: Any) -> Any:
        if isinstance(data, BaseModel):
            raw = data.model_dump()
        elif isinstance(data, dict):
            raw = data
        else:
            return data

        # 1. service_name limit
        service_name = raw.get("service_name")
        if service_name is not None and len(str(service_name)) > MAX_SERVICE_NAME_LENGTH:
            raise ValueError(
                f"service_name length ({len(str(service_name))}) exceeds limit of {MAX_SERVICE_NAME_LENGTH}"
            )

        # 2. message limit
        message = raw.get("message")
        if message is not None and len(str(message)) > MAX_MESSAGE_LENGTH:
            raise ValueError(
                f"message length ({len(str(message))}) exceeds limit of {MAX_MESSAGE_LENGTH}"
            )

        # 3. stack_trace limit
        stack_trace = raw.get("stack_trace")
        if stack_trace is not None and len(str(stack_trace)) > MAX_STACK_TRACE_LENGTH:
            raise ValueError(
                f"stack_trace length ({len(str(stack_trace))}) exceeds limit of {MAX_STACK_TRACE_LENGTH}"
            )

        # 4. extra fields count and size limits
        extra_keys = [k for k in raw.keys() if k not in _CORE_FIELDS]
        if len(extra_keys) > MAX_EXTRA_FIELDS:
            raise ValueError(
                f"Number of extra fields ({len(extra_keys)}) exceeds limit of {MAX_EXTRA_FIELDS}"
            )

        total_extra_size = 0
        for key in extra_keys:
            key_str = str(key)
            if len(key_str) > MAX_EXTRA_KEY_LENGTH:
                raise ValueError(
                    f"Extra field key '{key_str[:20]}' exceeds limit of {MAX_EXTRA_KEY_LENGTH}"
                )
            val = raw[key]
            val_size = len(val) if isinstance(val, str) else len(str(val))
            if val_size > MAX_EXTRA_VALUE_LENGTH:
                raise ValueError(
                    f"Extra field '{key_str}' value size ({val_size}) exceeds limit of {MAX_EXTRA_VALUE_LENGTH}"
                )
            total_extra_size += len(key_str) + val_size

        if total_extra_size > MAX_TOTAL_EXTRA_SIZE:
            raise ValueError(
                f"Total size of extra fields ({total_extra_size}) exceeds limit of {MAX_TOTAL_EXTRA_SIZE}"
            )

        return data
