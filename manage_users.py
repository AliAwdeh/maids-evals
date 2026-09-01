#!/usr/bin/env python3
"""Add, list, and revoke platform access tokens."""

import argparse
import sys

from auth import (
    add_user,
    env_admin_username,
    is_admin,
    list_users,
    reset_user_token,
    revoke_user,
    USERS_PATH,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Maids Evals access tokens.")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="Create a user and print their token once.")
    add_p.add_argument("username")

    sub.add_parser("list", help="List usernames.")

    rev_p = sub.add_parser("revoke", help="Remove a user. Their token stops working immediately.")
    rev_p.add_argument("username")

    reset_p = sub.add_parser(
        "reset-token", help="Regenerate a user's token and print the new one once."
    )
    reset_p.add_argument("username")

    args = parser.parse_args()

    if args.command == "add":
        try:
            token = add_user(args.username)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Created user {args.username!r}.")
        print(f"Token file: {USERS_PATH}")
        print()
        print("Give them this token once. It cannot be shown again:")
        print(token)
        return 0

    if args.command == "list":
        users = list_users()
        admin = env_admin_username()
        if not users:
            print("No users. Set ADMIN_USERNAME / ADMIN_TOKEN in .env, or: python manage_users.py add <username>")
            return 0
        for name in users:
            if admin and name == admin:
                suffix = " (env admin)"
            elif is_admin(name):
                suffix = " (admin)"
            else:
                suffix = ""
            print(f"{name}{suffix}")
        return 0

    if args.command == "revoke":
        try:
            revoked = revoke_user(args.username)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if revoked:
            print(f"Revoked {args.username!r}. Existing sessions stop on next request.")
            return 0
        print(f"error: no user named {args.username!r}", file=sys.stderr)
        return 1

    if args.command == "reset-token":
        try:
            token = reset_user_token(args.username)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"Reset token for {args.username!r}. The old token stops working immediately.")
        print()
        print("Give them this token once. It cannot be shown again:")
        print(token)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
