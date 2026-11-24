"""Inspect and manage the SQLite notification queue from the repository root."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from pipip.config import ConfigError, DatabaseSettings, load_config
from pipip.core.types import Notification
from pipip.db.models import NotificationOutbox
from pipip.db.session import Database
from pipip.outputs.presentation import format_timestamp

_STATUSES = ("pending", "failed", "sent", "dead")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    database = parser.add_mutually_exclusive_group()
    database.add_argument("--config", type=Path, help="Read the database URL from this YAML file")
    database.add_argument("--db", type=Path, help="Path to an existing pi-pip! SQLite database")

    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("list", help="Show queue entries, newest first")
    show.add_argument("--status", choices=_STATUSES)

    add = commands.add_parser("add", help="Queue a test notification for the running pi-pip!")
    add.add_argument("--notifier", choices=("ntfy",), default="ntfy")
    add.add_argument("--title", default="pi-pip! notification test")
    add.add_argument("--message", default="Test notification from the pi-pip! queue.")
    add.add_argument("--target", default="pipip-test", help="Name associated with the queue entry")

    clear = commands.add_parser("clear", help="Delete queue entries")
    clear.add_argument("--status", choices=_STATUSES, help="Delete only entries with this status")
    return parser.parse_args()


async def manage_queue(args: argparse.Namespace) -> None:
    settings = (
        DatabaseSettings(url=f"sqlite:///{args.db.expanduser().resolve()}")
        if args.db is not None
        else load_config(args.config).database
    )
    database = Database(settings)
    try:
        if database.path is None:
            raise ConfigError("The notification queue requires a file-backed SQLite database.")
        if not database.path.is_file():
            raise ConfigError(f"SQLite database does not exist: {database.path}")

        async with database.session() as session:
            if args.command == "add":
                notification = Notification(
                    app_name=args.target,
                    source="manual",
                    old_version=None,
                    new_version="test",
                    title_override=args.title,
                    message_override=args.message,
                    event_type="test",
                )
                row = NotificationOutbox(
                    idempotency_key=f"test:{uuid4()}",
                    notifier=args.notifier,
                    app_name=notification.app_name,
                    event_type=notification.event_type,
                    payload=asdict(notification),
                )
                session.add(row)
                await session.flush()
                result = f"Queued #{row.id} for {row.notifier}."
            elif args.command == "clear":
                deletion = delete(NotificationOutbox)
                if args.status:
                    deletion = deletion.where(NotificationOutbox.status == args.status)
                connection = await session.connection()
                deleted = await connection.execute(deletion)
                result = f"Deleted {deleted.rowcount} queue entries."
            else:
                statement = select(NotificationOutbox).order_by(NotificationOutbox.id.desc())
                if args.status:
                    statement = statement.where(NotificationOutbox.status == args.status)
                rows = (await session.scalars(statement)).all()
                lines = []
                for row in rows:
                    line = (
                        f"#{row.id} {row.status} {row.notifier} {row.app_name} "
                        f"({row.event_type}, attempts: {row.attempts}) "
                        f"created: {format_timestamp(row.created_at)}"
                    )
                    if row.status in {"pending", "failed"}:
                        line += f" · next attempt: {format_timestamp(row.next_attempt_at)}"
                    if row.last_error:
                        line += f" · {row.last_error}"
                    lines.append(line)
                result = "\n".join(lines) or "No queue entries."
        print(result)
    finally:
        await database.dispose()


def main() -> None:
    try:
        asyncio.run(manage_queue(parse_args()))
    except (ConfigError, SQLAlchemyError) as error:
        raise SystemExit(f"Queue error: {error}") from error


if __name__ == "__main__":
    main()
