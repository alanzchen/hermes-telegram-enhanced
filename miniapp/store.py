"""Private atomic JSON store. One writer process per profile/state directory."""

from __future__ import annotations
import copy
import json
import os
from pathlib import Path
import secrets
import threading
import time
from contextlib import contextmanager
from .spec import SpecError, validate_spec, text, number

MAX_STORE_BYTES = 20 * 1024 * 1024
MAX_RESULTS = 128


class SpecStore:
    def __init__(self, path, max_records=256):
        self.path = Path(path)
        self.max_records = max_records
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > MAX_STORE_BYTES:
            raise SpecError("Mini App store exceeds size cap")
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, UnicodeError) as exc:
            raise SpecError(
                "Mini App store is corrupt; refusing to overwrite it"
            ) from exc

    def _write(self, data):
        raw = json.dumps(
            data, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode()
        if len(raw) > MAX_STORE_BYTES:
            raise SpecError("Mini App store full")
        tmp = self.path.with_name(self.path.name + "." + secrets.token_hex(6) + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                tmp.unlink()

    @contextmanager
    def transaction(self):
        with self.lock:
            data = self._read()
            yield data
            self._write(data)

    def get(self, id):
        with self.lock:
            return copy.deepcopy(self._read().get(id))

    @staticmethod
    def owned(rec, target):
        if not rec or any(
            str(rec.get(k) or "") != str(target.get(k) or "")
            for k in ("chat_id", "thread_id", "user_id", "session_key")
        ):
            raise SpecError("No Mini App in the current conversation with that id")

    def create(self, spec, title, target, expires_in=3600):
        spec = validate_spec(spec)
        text(title, "title", 200, False)
        if type(expires_in) is not int:
            raise SpecError("expires_in must be integer seconds")
        number(expires_in, "expires_in", 60, 86400)
        if not all(target.get(k) for k in ("chat_id", "user_id", "session_key")):
            raise SpecError("No bound Telegram user/conversation")
        now = time.time()
        rec = dict(
            id=secrets.token_urlsafe(18),
            spec=spec,
            title=title,
            created_at=now,
            expires_at=now + expires_in,
            revision=1,
            closed=False,
            results={},
            receipts={},
            message_id=None,
            **{
                k: str(target.get(k) or "")
                for k in ("chat_id", "thread_id", "user_id", "session_key", "chat_type")
            },
        )
        with self.transaction() as data:
            for k in [k for k, v in data.items() if v["expires_at"] <= now]:
                data.pop(k)
            if len(data) >= self.max_records:
                raise SpecError(
                    "Mini App store full; wait for existing views to expire"
                )
            data[rec["id"]] = rec
        return copy.deepcopy(rec)

    def update(self, id, spec, target):
        spec = validate_spec(spec)
        with self.transaction() as data:
            rec = data.get(id)
            self.owned(rec, target)
            if rec["closed"] or rec["expires_at"] <= time.time():
                raise SpecError("Mini App is closed or expired")
            if any(
                r["status"] in ("pending", "uncertain") for r in rec["results"].values()
            ):
                raise SpecError(
                    "Submission is in flight or uncertain; reconcile it before replacing the spec"
                )
            rec["spec"] = spec
            rec["revision"] += 1
            # Keep completed one-time claims across updates: never silently re-authorize.
            result = copy.deepcopy(rec)
        return result

    def close(self, id, target):
        with self.transaction() as data:
            rec = data.get(id)
            self.owned(rec, target)
            rec["closed"] = True
            rec["revision"] += 1
            return copy.deepcopy(rec)

    def message(self, id, message_id):
        with self.transaction() as data:
            data[id]["message_id"] = message_id

    def sweep(self):
        with self.lock:
            data = self._read()
            now = time.time()
            expired = [k for k, v in data.items() if v["expires_at"] <= now]
            if expired:
                for k in expired:
                    data.pop(k)
                self._write(data)


def public_record(rec):
    return {
        k: copy.deepcopy(rec[k])
        for k in ("id", "title", "spec", "revision", "closed", "expires_at", "results")
    }
