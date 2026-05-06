"""CLI entrypoint.

Subcommands:

  cupid run          -- main loop: watch + auto-book (long-running)
  cupid once         -- one polling tick, then exit (good for cron/testing)
  cupid notify-test  -- send a test notification through every configured channel
  cupid record       -- open a real browser to record selectors
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import click
import structlog
from dotenv import load_dotenv

from .config import Secrets, load_config
from .notify import Notice, Notifier
from .scheduler import Scheduler
from .state import State


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stdout)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
    )


@click.group()
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to config.yaml. Defaults to $CONFIG_PATH or ./config.yaml",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None) -> None:
    load_dotenv()
    _setup_logging()
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@cli.command("run")
@click.pass_context
def run_cmd(ctx: click.Context) -> None:
    """Run the scheduler loop forever."""
    cfg = load_config(ctx.obj["config_path"])
    secrets = Secrets.from_env()
    state = State()
    s = Scheduler(cfg, secrets, state)
    asyncio.run(s.run_forever())


@cli.command("once")
@click.pass_context
def once_cmd(ctx: click.Context) -> None:
    """Run a single tick and exit."""
    cfg = load_config(ctx.obj["config_path"])
    secrets = Secrets.from_env()
    state = State()
    s = Scheduler(cfg, secrets, state)
    asyncio.run(s.tick())


@cli.command("notify-test")
@click.pass_context
def notify_test_cmd(ctx: click.Context) -> None:
    """Send a test message via every configured channel."""
    cfg = load_config(ctx.obj["config_path"])
    secrets = Secrets.from_env()
    Notifier(cfg, secrets).send(
        Notice(
            subject="Cupid notification test",
            body="If you see this, channel is wired up.",
        )
    )


@cli.command("record")
@click.option(
    "--kind",
    type=click.Choice(["license", "ceremony"]),
    default="ceremony",
    help="Which booking flow to record.",
)
def record_cmd(kind: str) -> None:
    """Open a real browser so you can step through the flow and the
    script logs selectors as you click. Use this once to update
    cupid/scraper.py SELECTORS for current Salesforce markup."""
    from .scraper import run_record

    run_record(kind)  # type: ignore[arg-type]


if __name__ == "__main__":
    cli()
