"""Local-only owner bootstrap: python -m mails_api.cli create-owner USERNAME."""

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
    args = parser.parse_args(argv)
    engine = create_engine(Settings().database_url)
    try:
        with Session(engine) as db:
            if db.scalar(select(User).where(User.role == "owner")) is not None:
                print("An owner already exists.", file=sys.stderr)
                return 1
            password = getpass.getpass("Password: ")
            confirmation = getpass.getpass("Confirm password: ")
            if password != confirmation:
                print("Passwords do not match.", file=sys.stderr)
                return 1
            db.add(
                User(
                    username=args.username,
                    password_hash=hash_password(password),
                    role="owner",
                )
            )
            db.commit()
        print("Owner created.")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
