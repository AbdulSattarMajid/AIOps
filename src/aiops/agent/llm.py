import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMReply:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


class MissingAPIKeyError(RuntimeError):
    pass


class MissingModelError(RuntimeError):
    pass


class GroqLLM:
    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 fallback_model: Optional[str] = None,
                 max_tokens: int = 4096,
                 client=None):
        key = api_key or os.getenv("GROQ_API_KEY")
        if client is None and not key:
            raise MissingAPIKeyError(
                "GROQ_API_KEY is not set. Create a key at console.groq.com "
                "and add it to your .env file."
            )
        self.model = model or os.getenv("LLM_MODEL")
        if not self.model:
            raise MissingModelError("Set LLM_MODEL in your .env file.")
        self.fallback_model = fallback_model or os.getenv("LLM_FALLBACK_MODEL")
        self.max_tokens = max_tokens
        # gpt-oss models are reasoning models; thinking counts toward max_tokens,
        # so keep max_tokens high (4096).
        if client is None:
            from groq import Groq

            client = Groq(api_key=key)
        self.client = client

    def complete(self, system: str, user: str) -> LLMReply:
        try:
            return self._complete_with_model(self.model, system, user)
        except Exception:
            if not self.fallback_model or self.fallback_model == self.model:
                raise
            print(
                "Warning: primary Groq model failed; retrying with the "
                "configured fallback model."
            )
            return self._complete_with_model(
                self.fallback_model, system, user
            )

    def _complete_with_model(self, model: str, system: str,
                             user: str) -> LLMReply:
        response = self.client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self.max_tokens,
            temperature=0.2,
        )
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        return LLMReply(
            text=choice.message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=model,
        )
