from aiops.dedup.normalize import normalize_trace


def test_http_status_codes_remain_distinct():
    pairs = [
        ("HTTPError: 404 Client Error", "HTTPError: 500 Client Error"),
        ("HTTP/1.1 404", "HTTP/1.1 500"),
        ("status_code=404", "status_code=500"),
    ]
    for first, second in pairs:
        assert normalize_trace(first) != normalize_trace(second)
    assert normalize_trace("status 404") != normalize_trace("status 500")


def test_changing_values_still_collapse():
    first = (
        "line 19 at 2026-11-02T10:00:00Z pid=1234 "
        "request_id=123e4567-e89b-12d3-a456-426614174000 address=0x7f3a"
    )
    second = (
        "line 97 at 2027-01-05T23:59:59Z pid=9876 "
        "request_id=223e4567-e89b-12d3-a456-426614174001 address=0x9bb1"
    )
    assert normalize_trace(first) == normalize_trace(second)


def test_normalization_collapses_whitespace_and_other_numbers():
    assert normalize_trace("value 17\n\tand 42") == normalize_trace("value 93 and 81")

