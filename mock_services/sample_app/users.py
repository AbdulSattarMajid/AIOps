"""Sample user-service code with a deliberate bug, used to demo crash analysis."""

USERS = {1: "ana@example.com"}


class User:
    def __init__(self, email):
        self.email = email


def find_user(user_id):
    if user_id in USERS:
        return User(USERS[user_id])
    return None


def get_user_email(user_id):
    user = find_user(user_id)
    return user.email
