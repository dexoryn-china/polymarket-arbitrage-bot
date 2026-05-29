"""Optional Sentry error tracking integration.

Initialises Sentry SDK when SENTRY_DSN is set in environment.
Safe to import even if sentry-sdk is not installed.
"""

from __future__ import annotations

import os

from src.observability.logger import get_logger

log = get_logger(__name__)


def init_sentry() -> bool:
    """Initialise Sentry if SENTRY_DSN is configured. Returns True if active."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        sentry_sdk.init(
            dsn=dsn,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.1,
            profiles_sample_rate=0.1,
            environment=os.environ.get("ENVIRONMENT", "production"),
            release=os.environ.get("BOT_VERSION", "0.2.0"),
            # Scrub sensitive data
            before_send=_scrub_event,
        )
        log.info("sentry.initialised")
        return True
    except ImportError:
        log.warning("sentry.not_installed", msg="pip install sentry-sdk[flask]")
        return False
    except Exception as e:
        log.error("sentry.init_failed", error=str(e))
        return False


def _scrub_event(event: dict, hint: dict) -> dict:
    """Remove sensitive data from Sentry events."""
    sensitive_keys = {
        "api_key", "api_secret", "passphrase", "private_key",
        "password", "token", "secret", "mnemonic",
    }
    if "extra" in event:
        for key in list(event["extra"].keys()):
            if any(s in key.lower() for s in sensitive_keys):
                event["extra"][key] = "***REDACTED***"
    return event
