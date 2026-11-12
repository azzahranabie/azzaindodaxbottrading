import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class StorageError(RuntimeError):
    pass


class LockBusy(RuntimeError):
    pass


class LocalStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("BOT_STATE_FILE", ".bot_data.json"))
        self._lock = threading.Lock()
        self._tick_lock = threading.Lock()

    def _read(self):
        if not self.path.exists():
            return {"state": None, "events": [], "snapshots": [], "kill_switch": False}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StorageError(f"State lokal tidak dapat dibaca: {error}") from error
        payload.setdefault("snapshots", [])
        return payload

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".bot-state-", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise StorageError(f"State lokal tidak dapat disimpan: {error}") from error

    def acquire_tick_lock(self, ttl_seconds=300):
        if not self._tick_lock.acquire(blocking=False):
            raise LockBusy("Tick sedang berjalan")
        return "local"

    def release_tick_lock(self, token):
        if token == "local" and self._tick_lock.locked():
            self._tick_lock.release()

    def load_state(self):
        return self._read()["state"]

    def save_state(self, state):
        with self._lock:
            payload = self._read()
            payload["state"] = state
            self._write(payload)

    def read_events(self, limit=10_000):
        return self._read()["events"][:limit]

    def is_kill_switch_on(self):
        return bool(self._read()["kill_switch"])

    def set_kill_switch(self, enabled):
        with self._lock:
            payload = self._read()
            payload["kill_switch"] = bool(enabled)
            self._write(payload)

    def commit(self, state, event, symbol, candle_time):
        with self._lock:
            payload = self._read()
            processed = (payload["state"] or {}).get("processed", {})
            if candle_time <= processed.get(symbol, -1):
                return False
            state["processed"][symbol] = candle_time
            payload["state"] = state
            payload["events"].insert(0, event)
            payload["events"] = payload["events"][:10_000]
            self._write(payload)
        return True

    def append_event(self, event):
        with self._lock:
            payload = self._read()
            payload["events"].insert(0, event)
            payload["events"] = payload["events"][:10_000]
            self._write(payload)

    def append_snapshot(self, snapshot):
        with self._lock:
            payload = self._read()
            payload["snapshots"].insert(0, snapshot)
            payload["snapshots"] = payload["snapshots"][:10_000]
            self._write(payload)

    def read_snapshots(self, limit=10_000):
        return self._read()["snapshots"][:limit]


class UpstashStore:
    """Persistent state and logs for Vercel's stateless function runtime."""

    _COMMIT_SCRIPT = (
        "if redis.call('EXISTS',KEYS[3]) == 1 then return 0 end "
        "redis.call('SET',KEYS[3],'1','EX',ARGV[3]); "
        "redis.call('SET',KEYS[1],ARGV[1]); "
        "redis.call('LPUSH',KEYS[2],ARGV[2]); "
        "redis.call('LTRIM',KEYS[2],0,9999); return 1"
    )
    _RELEASE_SCRIPT = (
        "if redis.call('GET',KEYS[1]) == ARGV[1] then "
        "return redis.call('DEL',KEYS[1]) else return 0 end"
    )

    def __init__(self, url=None, token=None, prefix=None):
        self.url = (url or os.getenv("UPSTASH_REDIS_REST_URL", "")).rstrip("/")
        self.token = token or os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
        self.prefix = prefix or os.getenv("UPSTASH_KEY_PREFIX", "azza-indodax")
        if not self.url or not self.token:
            raise StorageError("UPSTASH_REDIS_REST_URL dan UPSTASH_REDIS_REST_TOKEN wajib diisi")

    def _command(self, *args):
        request = Request(
            self.url,
            data=json.dumps(list(args)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise StorageError(f"Upstash REST gagal: {error}") from error
        if payload.get("error"):
            raise StorageError(f"Upstash menolak perintah: {payload['error']}")
        return payload.get("result")

    def _key(self, suffix):
        return f"{self.prefix}:{suffix}"

    def acquire_tick_lock(self, ttl_seconds=300):
        token = uuid.uuid4().hex
        result = self._command("SET", self._key("tick-lock"), token, "NX", "EX", ttl_seconds)
        if result != "OK":
            raise LockBusy("Tick sedang berjalan di instance lain")
        return token

    def release_tick_lock(self, token):
        self._command("EVAL", self._RELEASE_SCRIPT, 1, self._key("tick-lock"), token)

    def load_state(self):
        value = self._command("GET", self._key("state"))
        if value is None:
            return None
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise StorageError("State Redis bukan JSON valid") from error

    def save_state(self, state):
        self._command("SET", self._key("state"), json.dumps(state, separators=(",", ":")))

    def read_events(self, limit=10_000):
        value = self._command("LRANGE", self._key("events"), 0, max(0, limit - 1))
        if not isinstance(value, list):
            raise StorageError("Log Redis tidak dapat dibaca")
        try:
            return [json.loads(item) for item in value]
        except (TypeError, json.JSONDecodeError) as error:
            raise StorageError("Ada baris log Redis yang bukan JSON valid") from error

    def is_kill_switch_on(self):
        return self._command("GET", self._key("kill-switch")) == "on"

    def set_kill_switch(self, enabled):
        self._command("SET", self._key("kill-switch"), "on" if enabled else "off")

    def commit(self, state, event, symbol, candle_time):
        processed_key = self._key(f"processed:{symbol}:{candle_time}")
        result = self._command(
            "EVAL",
            self._COMMIT_SCRIPT,
            3,
            self._key("state"),
            self._key("events"),
            processed_key,
            json.dumps(state, separators=(",", ":")),
            json.dumps(event, separators=(",", ":")),
            60 * 60 * 24 * 120,
        )
        return result == 1

    def append_event(self, event):
        self._command("LPUSH", self._key("events"), json.dumps(event, separators=(",", ":")))
        self._command("LTRIM", self._key("events"), 0, 9_999)

    def append_snapshot(self, snapshot):
        self._command(
            "LPUSH", self._key("snapshots"), json.dumps(snapshot, separators=(",", ":"))
        )
        self._command("LTRIM", self._key("snapshots"), 0, 9_999)

    def read_snapshots(self, limit=10_000):
        value = self._command("LRANGE", self._key("snapshots"), 0, max(0, limit - 1))
        if not isinstance(value, list):
            raise StorageError("Snapshot Redis tidak dapat dibaca")
        try:
            return [json.loads(item) for item in value]
        except (TypeError, json.JSONDecodeError) as error:
            raise StorageError("Ada snapshot Redis yang bukan JSON valid") from error


def create_store():
    if os.getenv("VERCEL") == "1" or os.getenv("BOT_STORAGE", "").lower() == "upstash":
        return UpstashStore()
    return LocalStore()
