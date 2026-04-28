# Copyright 2024-2026 Lager Data LLC
# SPDX-License-Identifier: Apache-2.0

"""
    cli.telemetry

    Best-effort capture of CLI command invocations.

    Posts a single event to a per-box telemetry endpoint after each command
    finishes. The endpoint lives on the Stout sidecar (port 9001); when no
    Stout is running on the target box the POST is a connection refusal that
    we silently swallow. The CLI never blocks on telemetry — a failed or slow
    endpoint must not affect the command's exit behavior.

    Captured fields are intentionally narrow: the resolved Click command path
    (e.g. "supply voltage", "python", "uart stream"), exit code, duration,
    cli version, and the user identifier already used for box locking. No raw
    arguments, net names, IPs, or script contents are ever transmitted.
"""

import os
import sys
import threading
import time
from datetime import datetime, timezone

DEFAULT_PORT = 9001
DEFAULT_PATH = '/telemetry/events'
DEFAULT_TIMEOUT = 0.5


def _resolve_command_path(ctx) -> str:
    """Return a dotted command path for telemetry.

    Walks the Click context chain so we capture nested subcommands (e.g.
    ``supply voltage``) rather than just the top-level group. Falls back to
    parsing ``sys.argv`` when Click's chain doesn't reach the leaf.
    """
    try:
        import click
        # Prefer the deepest live context — the leaf command.
        cur = click.get_current_context(silent=True) or ctx
        names = []
        while cur is not None and cur.parent is not None:
            if cur.info_name:
                names.append(cur.info_name)
            cur = cur.parent
        names.reverse()
        if names:
            return ' '.join(names)
    except Exception:
        pass

    # Fallback: take the first 1-3 non-flag tokens from argv.
    tokens = []
    for arg in sys.argv[1:]:
        if arg.startswith('-'):
            break
        tokens.append(arg)
        if len(tokens) >= 3:
            break
    return ' '.join(tokens) if tokens else 'unknown'


def _post_in_background(url: str, payload: dict) -> None:
    def _send():
        try:
            import requests
            requests.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        except Exception:
            # Telemetry is fire-and-forget — never let a failure surface.
            pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def record_invocation(ctx, box_ip: str, start_time: float) -> None:
    """Build the payload and dispatch it without blocking the caller.

    ``start_time`` is a ``time.monotonic()`` reading taken at the start of the
    command. Exit code is inferred from the active exception (if any) — 0 when
    closing cleanly, 1 when unwinding from any exception. Click's own
    ``SystemExit`` paths still go through here because ``ctx.call_on_close``
    runs during context teardown.
    """
    if not box_ip:
        return

    duration_ms = int(max(0, (time.monotonic() - start_time) * 1000))
    exit_code = 1 if sys.exc_info()[0] is not None else 0

    try:
        from . import __version__
        cli_version = __version__
    except Exception:
        cli_version = None

    try:
        from .box_storage import get_lager_user
        user_identifier = get_lager_user()
    except Exception:
        user_identifier = 'unknown'

    payload = {
        'commandPath': _resolve_command_path(ctx),
        'exitCode': exit_code,
        'durationMs': duration_ms,
        'cliVersion': cli_version,
        'userIdentifier': user_identifier,
        'occurredAt': datetime.now(timezone.utc).isoformat(),
    }

    port = int(os.environ.get('LAGER_TELEMETRY_PORT', DEFAULT_PORT))
    url = f"http://{box_ip}:{port}{DEFAULT_PATH}"
    _post_in_background(url, payload)
