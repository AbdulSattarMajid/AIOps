from typing import Dict, List

from aiops.agent.llm import LLMReply


class FakeLLM:
    def __init__(self, rules: Dict[str, List[str]]):
        self.rules = rules
        self.calls = []
        self._positions = {}

    def complete(self, system: str, user: str) -> LLMReply:
        self.calls.append((system, user))
        for key, replies in self.rules.items():
            if key in user:
                position = self._positions.get(key, 0)
                reply = replies[min(position, len(replies) - 1)] if replies else ""
                self._positions[key] = position + 1
                if isinstance(reply, Exception):
                    raise reply
                text = reply
                return LLMReply(text, len(text) // 4, len(text) // 4, "fake")
        text = "NO_PATCH: no scripted answer"
        return LLMReply(text, len(text) // 4, len(text) // 4, "fake")


def make_demo_llm() -> FakeLLM:
    return FakeLLM({
        "def get_user_email": [
            (
                "--- a/users.py\n+++ b/users.py\n@@ -99,3 +99,4 @@\n"
                "     user = find_user(account_id)\n"
                "-    return user.email\n"
                "+    if user is None:\n"
                "+        raise LookupError(f\"user {user_id} not found\")\n"
                "+    return user.email\n"
            ),
            (
                "--- a/users.py\n+++ b/users.py\n@@ -16,4 +16,6 @@\n"
                " def get_user_email(user_id):\n"
                "     user = find_user(user_id)\n"
                "+    if user is None:\n"
                "+        raise LookupError(f\"user {user_id} not found\")\n"
                "     return user.email\n"
            ),
        ],
        "def load_config": [
            (
                "--- a/config.py\n+++ b/config.py\n@@ -4,3 +4,5 @@\n"
                " def load_config():\n"
                '-    key = os.environ["STRIPE_API_KEY"]\n'
                '+    key = os.environ.get("STRIPE_API_KEY")\n'
                "+    if key is None:\n"
                '+        raise RuntimeError("Missing required environment variable: '
                'STRIPE_API_KEY")\n'
                "     return {\"stripe_key\": key}\n"
            ),
        ],
        "def get_connection": [
            (
                "NO_PATCH: Pool timeouts come from capacity or leaked connections "
                "elsewhere; "
                "restart or scaling is an operations action that needs human approval."
            )
        ],
    })


def make_demo_llm_4b() -> FakeLLM:
    return FakeLLM({
        "def get_user_email": [
            (
                "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,3 @@\n"
                " def get_user_email(user_id):\n"
                "     user = find_user(user_id)\n"
                "-    return user.email\n"
                "+    return user.email if user is not None else None\n"
            ),
            (
                "--- a/users.py\n+++ b/users.py\n@@ -17,3 +17,5 @@\n"
                " def get_user_email(user_id):\n"
                "     user = find_user(user_id)\n"
                "+    if user is None:\n"
                "+        raise LookupError(f\"user {user_id} not found\")\n"
                "     return user.email\n"
            ),
        ],
        "def load_config": [
            (
                "--- a/config.py\n+++ b/config.py\n@@ -4,3 +4,5 @@\n"
                " def load_config():\n"
                '-    key = os.environ["STRIPE_API_KEY"]\n'
                '+    key = os.environ.get("STRIPE_API_KEY")\n'
                "+    if key is None:\n"
                '+        raise RuntimeError("Missing required environment variable: '
                'STRIPE_API_KEY")\n'
                "     return {\"stripe_key\": key}\n"
            ),
        ],
        "def get_connection": [
            (
                "--- a/db.py\n+++ b/db.py\n@@ -8,3 +8,5 @@\n"
                "     def get_connection(self):\n"
                "+        try:\n"
                "-        conn = self.pool.acquire(timeout=30)\n"
                "+            conn = self.pool.acquire(timeout=30)\n"
                "+        except TimeoutError as error:\n"
                '+            raise RuntimeError("database temporarily unavailable") '
                "from error\n"
                "         return conn\n"
            ),
            (
                "NO_PATCH: Pool timeouts come from capacity or leaked connections "
                "elsewhere; restart or scaling is an operations action that needs "
                "human approval."
            ),
        ],
    })
