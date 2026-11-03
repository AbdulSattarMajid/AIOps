import pytest
import users


def test_existing_user_email_still_works():
    assert users.get_user_email(1) == "ana@example.com"


def test_missing_user_gives_clear_error_not_attributeerror():
    with pytest.raises(Exception) as error:
        users.get_user_email(999)
    assert not isinstance(error.value, AttributeError)
