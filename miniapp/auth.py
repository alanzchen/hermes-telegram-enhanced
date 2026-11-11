"""Telegram HMAC verification (bot-token mode, including the signature field).

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
Do not use initDataUnsafe as identity. Query binding is not authentication: it is
accepted only with a valid signature AND the stored requesting user's identity.
"""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class AuthError(ValueError):
    pass


def verify_init_data(
    raw, bot_token, *, user_id, spec_id, query_id=None, now=None, max_age=3600
):
    if not isinstance(raw, str) or not raw or len(raw) > 8192 or not bot_token:
        raise AuthError("Open this page from its Telegram launch button.")
    try:
        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=32,
            errors="strict",
        )
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicate fields")
        supplied = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", supplied):
            raise ValueError("bad hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied.lower()):
            raise ValueError("bad hash")
        stamp = int(fields["auth_date"])
        now = time.time() if now is None else now
        if stamp > now + 30 or now - stamp > max_age:
            raise ValueError("stale auth_date")
        user = json.loads(fields["user"])
        if (
            not isinstance(user, dict)
            or type(user.get("id")) is not int
            or user["id"] <= 0
            or str(user["id"]) != str(user_id)
        ):
            raise ValueError("wrong user")
        if "start_param" in fields:
            if fields["start_param"] != spec_id:
                raise ValueError("wrong spec")
        elif query_id != spec_id:
            raise ValueError("missing spec binding")
        return dict(fields, user=user)
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise AuthError(
            "Telegram authentication expired or does not match this view. Reopen the launch button."
        ) from exc
