"""Local-only owner/member bootstrap commands."""

import argparse
import getpass
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mails_api.models import User
from mails_api.security.passwords import hash_password
from mails_api.settings import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mails local administration")
    commands = parser.add_subparsers(dest="command", required=True)
    owner_parser = commands.add_parser("create-owner")
    owner_parser.add_argument("username")
    member_parser = commands.add_parser("create-member")
    member_parser.add_argument("username")
    args = parser.parse_args(argv)
    if not args.username.strip() or len(args.username) > 128:
        print("Username must contain 1 to 128 characters.", file=sys.stderr)
        return 1
    engine = create_engine(Settings().database_url)
    try:
        with Session(engine) as db:
            if (
                args.command == "create-owner"
                and db.scalar(select(User).where(User.role == "owner")) is not None
            ):
                print("An owner already exists.", file=sys.stderr)
                return 1
            if (
                args.command == "create-member"
                and db.scalar(select(User).where(User.role == "owner")) is None
            ):
                print("An owner is required before creating a member.", file=sys.stderr)
                return 1
            if db.scalar(select(User).where(User.username == args.username)) is not None:
                print("That username already exists.", file=sys.stderr)
                return 1
            password = getpass.getpass("Password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if not password:
                print("Password must not be empty.", file=sys.stderr)
                return 1
            if password != confirmation:
                print("Passwords do not match.", file=sys.stderr)
                return 1
            db.add(
                User(
                    username=args.username,
                    password_hash=hash_password(password),
                    role="owner" if args.command == "create-owner" else "member",
                )
            )
            db.commit()
        print("Owner created." if args.command == "create-owner" else "Member created.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
