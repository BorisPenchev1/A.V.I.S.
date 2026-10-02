"""Identity for AVIS: users, roles, devices, and signed sessions.

This is the foundation for turning AVIS from a single-user assistant into a
multi-tenant home system. Every request carries a :class:`Principal` describing
*who* is asking, *from which device*, over *which network*, and how trusted that
device is. The permission policy in :mod:`security.security` reads those fields.

Password hashing uses stdlib ``hashlib.scrypt`` (no third-party dependency).
Sessions are opaque tokens backed by a server-side registry and HMAC-signed so a
tampered or guessed token is rejected before the registry is even consulted.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from store import db


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRET_PATH = Path(os.getenv("AVIS_SERVER_SECRET", str(PROJECT_ROOT / "security" / "server_secret")))

# Roles, most privileged first. Anything not listed is treated as "guest".
# "admin" is the system operator (dashboards, user management); "owner" is the
# head of household; the rest are family tiers.
ROLES = ("admin", "owner", "adult", "teen", "child", "guest")

# The fixed administrator account, seeded once on first run. Override the
# password via the AVIS_ADMIN_PASSWORD env var; change it after first login.
ADMIN_USER = os.getenv("AVIS_ADMIN_USER", "admin").strip().casefold()
ADMIN_DEFAULT_PASSWORD = os.getenv("AVIS_ADMIN_PASSWORD", "avis-admin")

# Role assigned to self-service sign-ups; an admin promotes from here.
DEFAULT_SIGNUP_ROLE = os.getenv("AVIS_SIGNUP_ROLE", "guest")

SESSION_TTL = int(os.getenv("AVIS_SESSION_TTL", str(12 * 3600)))  # seconds

# scrypt cost parameters (stdlib defaults tuned for an interactive login).
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
# scrypt needs roughly 128 * N * r bytes; give OpenSSL headroom above its 32MB
# default so the derivation is not rejected with "memory limit exceeded".
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * 2


@dataclass(frozen=True)
class Principal:
    """The authenticated context of a single request."""

    user: str = "owner"
    role: str = "owner"
    device_id: str = "local"
    source: str = "lan"      # "lan" | "remote"
    trust: str = "trusted"   # "trusted" | "new" | "quarantined"

    def role_rank(self) -> int:
        """Lower is more privileged; unknown roles fall to the bottom."""
        try:
            return ROLES.index(self.role)
        except ValueError:
            return len(ROLES)

    def at_least(self, role: str) -> bool:
        """True when this principal is at least as privileged as ``role``."""
        try:
            return self.role_rank() <= ROLES.index(role)
        except ValueError:
            return False


# The default principal keeps existing local callers (CLI, desktop app) working
# exactly as before: the owner, on a trusted LAN device.
OWNER = Principal()


# ---------------------------------------------------------------------------
# Server secret (used to sign session tokens)
# ---------------------------------------------------------------------------

def _server_secret() -> bytes:
    try:
        return SECRET_PATH.read_bytes()
    except OSError:
        secret = secrets.token_bytes(32)
        try:
            SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
            SECRET_PATH.write_bytes(secret)
            os.chmod(SECRET_PATH, 0o600)
        except OSError:
            pass
        return secret


# ---------------------------------------------------------------------------
# User store (backed by store.db)
# ---------------------------------------------------------------------------

def _hash_password(password: str, salt: bytes) -> str:
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return derived.hex()


def create_user(username: str, password: str, role: str = "adult") -> None:
    """Create or replace a user with a scrypt-hashed password."""
    username = username.strip().casefold()
    if not username or not password:
        raise ValueError("username and password are required")
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    salt = secrets.token_bytes(16)
    db.upsert_user(
        username, role, salt.hex(), _hash_password(password, salt),
        time.strftime("%Y-%m-%d"),
    )


def user_exists(username: str) -> bool:
    return db.get_user((username or "").strip().casefold()) is not None


def verify_user(username: str, password: str) -> dict[str, Any] | None:
    """Return the user record on a correct password, else ``None``.

    Runs a dummy hash on unknown users so timing does not reveal which usernames
    exist.
    """
    username = (username or "").strip().casefold()
    record = db.get_user(username)
    if record is None:
        # Constant-ish time: still derive a hash before failing.
        _hash_password(password or "", b"0" * 16)
        return None
    try:
        salt = bytes.fromhex(record["salt"])
        expected = record["hash"]
    except (KeyError, ValueError, TypeError):
        return None
    candidate = _hash_password(password or "", salt)
    if hmac.compare_digest(candidate, expected):
        return {"user": username, "role": record.get("role", "guest")}
    return None


def user_count() -> int:
    return db.count_users()


def ensure_admin() -> None:
    """Seed the fixed administrator account once, if it does not exist yet."""
    if db.get_user(ADMIN_USER) is None:
        create_user(ADMIN_USER, ADMIN_DEFAULT_PASSWORD, "admin")


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass
class _SessionRecord:
    principal: Principal
    expires: float
    extra: dict[str, Any] = field(default_factory=dict)


_SESSIONS: dict[str, _SessionRecord] = {}
_SESSIONS_LOCK = threading.Lock()


def _sign(session_id: str) -> str:
    mac = hmac.new(_server_secret(), session_id.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()[:32]


def issue_session(principal: Principal, ttl: int = SESSION_TTL) -> str:
    """Create a signed session token bound to ``principal``."""
    session_id = secrets.token_urlsafe(24)
    token = f"{session_id}.{_sign(session_id)}"
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = _SessionRecord(principal, time.time() + ttl)
    return token


def resolve_session(token: str | None, *, source: str = "remote") -> Principal | None:
    """Return the :class:`Principal` for a valid, unexpired, correctly signed token."""
    if not token or "." not in token:
        return None
    session_id, _, signature = token.partition(".")
    if not hmac.compare_digest(signature, _sign(session_id)):
        return None
    with _SESSIONS_LOCK:
        record = _SESSIONS.get(session_id)
        if record is None:
            return None
        if record.expires < time.time():
            _SESSIONS.pop(session_id, None)
            return None
    # Reflect where the request actually came from onto the principal.
    principal = record.principal
    if principal.source != source:
        principal = Principal(principal.user, principal.role, principal.device_id, source, principal.trust)
    return principal


def revoke_session(token: str | None) -> None:
    if not token or "." not in token:
        return
    session_id = token.partition(".")[0]
    with _SESSIONS_LOCK:
        _SESSIONS.pop(session_id, None)


def active_sessions() -> int:
    """Number of live, unexpired sessions (used by the admin dashboard)."""
    now = time.time()
    with _SESSIONS_LOCK:
        return sum(1 for r in _SESSIONS.values() if r.expires >= now)


# Seed the database and the fixed admin account on import so any entry point
# (mirror server, CLI, tests) starts with a usable store.
db.init_db()
ensure_admin()


# ---------------------------------------------------------------------------
# Command-line user management
#
#   python -m security.identity add <username> <role>      # prompts for password
#   python -m security.identity list
# ---------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import getpass

    if not argv or argv[0] in {"-h", "--help", "help"}:
        print("usage: python -m security.identity add <username> [role]\n"
              "       python -m security.identity list")
        return 0

    command = argv[0]
    if command == "add":
        if len(argv) < 2:
            print("add requires a username")
            return 2
        username = argv[1]
        role = argv[2] if len(argv) > 2 else "adult"
        if role not in ROLES:
            print(f"role must be one of {ROLES}")
            return 2
        password = getpass.getpass(f"Password for {username} ({role}): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("passwords did not match")
            return 2
        create_user(username, password, role)
        print(f"created user {username!r} with role {role!r}")
        return 0

    if command == "list":
        for rec in db.list_users():
            print(f"{rec['username']}\t{rec.get('role', 'guest')}\tcreated {rec.get('created', '?')}")
        return 0

    print(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
