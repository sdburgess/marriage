"""CLI entrypoint.

Subcommands:

  cupid run          -- main loop + web/Slack server (long-running)
  cupid once         -- one polling tick, then exit (good for cron/testing)
  cupid notify-test  -- send a test notification through every configured channel
  cupid record       -- open a real browser to record selectors
  cupid serve        -- run only the web/Slack server (no polling)
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
from .controller import Controller
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
    ctx.obj["config_path"] = config_path or os.getenv("CONFIG_PATH", "config.yaml")


@cli.command("run")
@click.pass_context
def run_cmd(ctx: click.Context) -> None:
    """Run the scheduler loop + web/Slack server forever."""
    config_path = ctx.obj["config_path"]
    cfg = load_config(config_path)
    secrets = Secrets.from_env()
    state = State()
    controller = Controller(cfg, secrets, state, config_path=config_path)
    scheduler = Scheduler(cfg, secrets, state, controller=controller)

    asyncio.run(_run_combined(controller, scheduler))


async def _run_combined(controller: Controller, scheduler: Scheduler) -> None:
    """Run the scheduler loop and uvicorn in the same event loop so they
    can share the Controller (live config, pause flag, pending Slack
    confirmations) without IPC."""
    import uvicorn

    from .server import build_app

    cfg = controller.cfg
    tasks: list[asyncio.Task] = [asyncio.create_task(scheduler.run_forever())]

    if cfg.server.enabled:
        app = build_app(controller)
        config = uvicorn.Config(
            app,
            host=cfg.server.host,
            port=cfg.server.port,
            log_level=os.getenv("LOG_LEVEL", "info").lower(),
            access_log=False,
        )
        server = uvicorn.Server(config)
        tasks.append(asyncio.create_task(server.serve()))

    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()


@cli.command("once")
@click.pass_context
def once_cmd(ctx: click.Context) -> None:
    """Run a single tick and exit. Skips the web server."""
    cfg = load_config(ctx.obj["config_path"])
    secrets = Secrets.from_env()
    state = State()
    s = Scheduler(cfg, secrets, state)
    asyncio.run(s.tick())


@cli.command("serve")
@click.pass_context
def serve_cmd(ctx: click.Context) -> None:
    """Run only the web/Slack server (no polling). Useful for debugging
    the dashboard without burning quota on the city site."""
    import uvicorn

    from .server import build_app

    config_path = ctx.obj["config_path"]
    cfg = load_config(config_path)
    secrets = Secrets.from_env()
    state = State()
    controller = Controller(cfg, secrets, state, config_path=config_path)
    uvicorn.run(
        build_app(controller),
        host=cfg.server.host,
        port=cfg.server.port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


@cli.command("notify-test")
@click.pass_context
def notify_test_cmd(ctx: click.Context) -> None:
    """Send a test message via every configured channel (SMS + email + Slack)."""
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
    script logs selectors as you click."""
    from .scraper import run_record

    run_record(kind)  # type: ignore[arg-type]


if __name__ == "__main__":
    cli()
