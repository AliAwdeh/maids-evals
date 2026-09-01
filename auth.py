import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
USERS_PATH = BASE_DIR / "users.json"
AUTH_COOKIE = "me_auth"
AUTH_SALT = "maids-evals-auth"
AUTH_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def env_admin_username() -> str:
    return (os.getenv("ADMIN_USERNAME") or "").strip()


def env_admin_token() -> str:
    return (os.getenv("ADMIN_TOKEN") or "").strip()


def _env_admin_user() -> Optional[Dict[str, str]]:
    username = env_admin_username()
    token = env_admin_token()
    if not username or not token:
        return None
    return {"username": username, "token_hash": hash_token(token), "source": "env"}


def hash_token(token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def verify_token(token: str, stored_hash: str) -> bool:
    if not token or not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def _empty_store() -> Dict[str, Any]:
    return {"users": []}


def load_users() -> Dict[str, Any]:
    if not USERS_PATH.is_file():
        return _empty_store()
    try:
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("users"), list):
        return _empty_store()
    return data


def save_users(data: Dict[str, Any]) -> None:
    tmp = USERS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, USERS_PATH)
    try:
        os.chmod(USERS_PATH, 0o600)
    except Exception:
        pass


def list_users() -> List[str]:
    names = [u.get("username", "") for u in load_users().get("users", []) if u.get("username")]
    admin = _env_admin_user()
    if admin and admin["username"] not in names:
        return [admin["username"]] + names
    return names


def find_user(username: str) -> Optional[Dict[str, str]]:
    wanted = (username or "").strip()
    if not wanted:
        return None
    admin = _env_admin_user()
    if admin and admin["username"] == wanted:
        return admin
    for user in load_users().get("users", []):
        if user.get("username") == wanted:
            return user
    return None


def is_admin(username: str) -> bool:
    """The env admin is always an admin. users.json users may opt in via an
    ``is_admin`` flag (defaults to false), reserved for future promoted admins."""
    wanted = (username or "").strip()
    if not wanted:
        return False
    admin = env_admin_username()
    if admin and wanted == admin:
        return True
    for user in load_users().get("users", []):
        if user.get("username") == wanted:
            return bool(user.get("is_admin", False))
    return False


def list_users_detailed() -> List[Dict[str, Any]]:
    """Usernames plus display metadata for admin UIs. Never returns token hashes
    or any other secret material."""
    detailed: List[Dict[str, Any]] = []
    admin = env_admin_username()
    if admin:
        detailed.append(
            {"username": admin, "is_env_admin": True, "is_admin": True, "removable": False}
        )
    for user in load_users().get("users", []):
        name = user.get("username")
        if not name:
            continue
        detailed.append(
            {
                "username": name,
                "is_env_admin": False,
                "is_admin": bool(user.get("is_admin", False)),
                "removable": True,
            }
        )
    return detailed


def add_user(username: str) -> str:
    username = (username or "").strip()
    if not username or not username.replace("_", "").replace("-", "").isalnum():
        raise ValueError("Username must be letters, numbers, hyphens, or underscores.")
    admin = _env_admin_user()
    if admin and username == admin["username"]:
        raise ValueError(
            f"User {username!r} is the env admin. Change ADMIN_USERNAME in .env instead."
        )
    store = load_users()
    if any(u.get("username") == username for u in store["users"]):
        raise ValueError(f"User {username!r} already exists.")
    token = secrets.token_urlsafe(32)
    store["users"].append({"username": username, "token_hash": hash_token(token)})
    save_users(store)
    return token


def revoke_user(username: str) -> bool:
    username = (username or "").strip()
    admin = _env_admin_user()
    if admin and username == admin["username"]:
        raise ValueError(
            f"{username!r} is the env admin. Change or remove ADMIN_USERNAME / ADMIN_TOKEN in .env."
        )
    store = load_users()
    before = len(store["users"])
    store["users"] = [u for u in store["users"] if u.get("username") != username]
    if len(store["users"]) == before:
        return False
    save_users(store)
    return True


def reset_user_token(username: str) -> str:
    """Generate a new token for an existing users.json user, replacing the old
    hash. Returns the plaintext token once. The env admin token lives in .env
    and cannot be reset here."""
    username = (username or "").strip()
    admin = _env_admin_user()
    if admin and username == admin["username"]:
        raise ValueError(
            f"{username!r} is the env admin. Change ADMIN_TOKEN in .env instead."
        )
    store = load_users()
    for user in store["users"]:
        if user.get("username") == username:
            token = secrets.token_urlsafe(32)
            user["token_hash"] = hash_token(token)
            save_users(store)
            return token
    raise ValueError(f"No user named {username!r}.")


def authenticate(username: str, token: str) -> Optional[str]:
    user = find_user(username)
    if not user:
        return None
    if not verify_token(token, user.get("token_hash", "")):
        return None
    return user["username"]


def auth_serializer(secret: str):
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(secret, salt=AUTH_SALT)


def dump_auth(secret: str, username: str) -> str:
    return auth_serializer(secret).dumps({"u": username})


def load_auth(secret: str, cookie: str) -> Optional[str]:
    if not cookie:
        return None
    try:
        data = auth_serializer(secret).loads(cookie, max_age=AUTH_MAX_AGE)
    except Exception:
        return None
    username = (data or {}).get("u")
    if not username or not find_user(username):
        return None
    return username
