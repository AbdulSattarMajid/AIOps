"""Week 5A demo: redact a sample log offline. No network calls."""
import sys

sys.path.insert(0, "src")

from aiops.security.redaction import redact_text


def main() -> int:
    password = "not-a-real-" + "password"
    email = "oncall.user@" + "example.test"
    groq_key = "gsk_" + "X" * 24
    sample = (
        "AttributeError: login failed for {email}\n"
        "File \"/app/users.py\", line 19, in get_user_email\n"
        "HTTP status 500\n"
        "password={password}\n"
        "GROQ_API_KEY={groq_key}\n"
    ).format(email=email, password=password, groq_key=groq_key)

    print("BEFORE REDACTION")
    print(sample)
    clean, findings = redact_text(sample)
    print("AFTER REDACTION")
    print(clean)
    print("FINDINGS (types and counts only)")
    print(findings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
