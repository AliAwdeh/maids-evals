"""Shared prompt catalogue with per-prompt access control."""

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from engine import required_placeholders
from storage import DATA_DIR, PROMPTS_DIR, _safe_prompt_name

CATALOGUE_DIR = DATA_DIR / "catalogue"
VISIBILITY = ("personal", "everyone", "selected")
ROLES = ("viewer", "editor")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _entry_dir(prompt_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "", prompt_id or "")
    if not safe:
        raise ValueError("Invalid prompt id.")
    return CATALOGUE_DIR / safe


def _meta_path(prompt_id: str) -> Path:
    return _entry_dir(prompt_id) / "meta.json"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _new_id(name: str) -> str:
    base = _safe_prompt_name(name)[:40]
    return f"{base}-{secrets.token_hex(3)}"


def _normalize_people(names: Optional[List[str]], owner: str) -> List[str]:
    out = []
    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name == owner or name in out:
            continue
        out.append(name)
    return out


def _normalize_change_log(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        out.append({
            "at": str(item.get("at") or ""),
            "by": str(item.get("by") or ""),
            "summary": summary,
            "purpose": str(item.get("purpose") or ""),
        })
    return out[-30:]


def _normalize_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    owner = str(meta.get("owner") or "").strip()
    visibility = str(meta.get("visibility") or "personal").strip().lower()
    if visibility not in VISIBILITY:
        visibility = "personal"
    everyone_role = str(meta.get("everyone_role") or "viewer").strip().lower()
    if everyone_role not in ROLES:
        everyone_role = "viewer"
    prompt_id = str(meta.get("id") or "")
    return {
        "id": prompt_id,
        "name": str(meta.get("name") or ""),
        "owner": owner,
        "visibility": visibility,
        "everyone_role": everyone_role,
        "viewers": _normalize_people(meta.get("viewers"), owner),
        "editors": _normalize_people(meta.get("editors"), owner),
        "edit_requests": _normalize_people(meta.get("edit_requests"), owner),
        "required_inputs": [str(x) for x in (meta.get("required_inputs") or []) if str(x).strip()],
        "content_column": str(meta.get("content_column") or ""),
        "created_at": str(meta.get("created_at") or ""),
        "updated_at": str(meta.get("updated_at") or ""),
        "cloned_from": str(meta.get("cloned_from") or ""),
        "conversation_id": str(meta.get("conversation_id") or prompt_id),
        "purpose": str(meta.get("purpose") or ""),
        "change_log": _normalize_change_log(meta.get("change_log")),
    }


def load_meta(prompt_id: str) -> Optional[Dict[str, Any]]:
    data = _read_json(_meta_path(prompt_id), None)
    if not isinstance(data, dict) or not data.get("id"):
        return None
    return _normalize_meta(data)


def load_body(prompt_id: str) -> Dict[str, str]:
    folder = _entry_dir(prompt_id)
    prompt = ""
    extra = ""
    p = folder / "prompt.txt"
    i = folder / "input_template.txt"
    if p.is_file():
        prompt = p.read_text(encoding="utf-8")
    if i.is_file():
        extra = i.read_text(encoding="utf-8")
    return {"prompt": prompt, "input_template": extra}


def role_for(meta: Dict[str, Any], username: str) -> Optional[str]:
    user = (username or "").strip()
    if not user:
        return None
    if user == meta.get("owner"):
        return "owner"
    if user in (meta.get("editors") or []):
        return "editor"
    if user in (meta.get("viewers") or []):
        return "viewer"
    if meta.get("visibility") == "everyone":
        return meta.get("everyone_role") or "viewer"
    return None


def can_view(meta: Dict[str, Any], username: str) -> bool:
    return role_for(meta, username) is not None


def can_edit(meta: Dict[str, Any], username: str) -> bool:
    role = role_for(meta, username)
    return role in ("owner", "editor")


def can_delete(meta: Dict[str, Any], username: str) -> bool:
    return (username or "").strip() == (meta.get("owner") or "")


def can_acl(meta: Dict[str, Any], username: str) -> bool:
    return can_delete(meta, username)


def _public_card(meta: Dict[str, Any], username: str) -> Dict[str, Any]:
    role = role_for(meta, username) or "viewer"
    return {
        **meta,
        "role": role,
        "mine": (username or "") == meta.get("owner"),
        "can_edit": can_edit(meta, username),
        "can_delete": can_delete(meta, username),
        "can_acl": can_acl(meta, username),
    }


def _iter_metas() -> List[Dict[str, Any]]:
    CATALOGUE_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for child in CATALOGUE_DIR.iterdir():
        if not child.is_dir() or child.name.startswith("_"):
            continue
        meta = load_meta(child.name)
        if meta:
            metas.append(meta)
    return metas


def list_visible(username: str) -> List[Dict[str, Any]]:
    migrate_legacy(username)
    cards = []
    for meta in _iter_metas():
        if not can_view(meta, username):
            continue
        cards.append(_public_card(meta, username))
    cards.sort(key=lambda c: (c.get("updated_at") or "", c.get("name") or ""), reverse=True)
    return cards


PAGE_SIZE = 12


def search_visible(
    username: str,
    q: str = "",
    owner: str = "",
    access: str = "",
    page: int = 1,
    page_size: int = PAGE_SIZE,
) -> Dict[str, Any]:
    items = list_visible(username)
    needle = (q or "").strip().lower()
    owner_q = (owner or "").strip().lower()
    access_q = (access or "").strip().lower()
    if needle:
        items = [
            i for i in items
            if needle in (i.get("name") or "").lower()
            or needle in (i.get("owner") or "").lower()
            or needle in (i.get("purpose") or "").lower()
            or needle in " ".join(i.get("required_inputs") or []).lower()
        ]
    if owner_q:
        items = [i for i in items if owner_q in (i.get("owner") or "").lower()]
    if access_q == "mine":
        items = [i for i in items if i.get("mine")]
    elif access_q == "shared":
        items = [i for i in items if (not i.get("mine")) and i.get("visibility") == "selected"]
    elif access_q == "everyone":
        items = [i for i in items if (not i.get("mine")) and i.get("visibility") == "everyone"]
    elif access_q in VISIBILITY:
        items = [i for i in items if i.get("visibility") == access_q]
    total = len(items)
    size = max(1, min(int(page_size or PAGE_SIZE), 50))
    pages = max(1, (total + size - 1) // size)
    current = max(1, min(int(page or 1), pages))
    start = (current - 1) * size
    return {
        "items": items[start:start + size],
        "total": total,
        "page": current,
        "pages": pages,
        "page_size": size,
    }


def get_visible(prompt_id: str, username: str) -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    if not meta or not can_view(meta, username):
        raise PermissionError("You cannot see this prompt.")
    body = load_body(prompt_id)
    return {**_public_card(meta, username), **body}


def create_prompt(
    username: str,
    name: str,
    prompt: str,
    input_template: str = "",
    visibility: str = "personal",
    everyone_role: str = "viewer",
    viewers: Optional[List[str]] = None,
    editors: Optional[List[str]] = None,
    content_column: str = "",
    cloned_from: str = "",
) -> Dict[str, Any]:
    owner = (username or "").strip()
    if not owner:
        raise ValueError("A prompt needs an owner.")
    display = (name or "").strip() or "untitled"
    prompt_id = _new_id(display)
    required = required_placeholders(prompt, input_template)
    meta = _normalize_meta({
        "id": prompt_id,
        "name": display,
        "owner": owner,
        "visibility": visibility,
        "everyone_role": everyone_role,
        "viewers": viewers or [],
        "editors": editors or [],
        "required_inputs": required,
        "content_column": content_column,
        "created_at": _now(),
        "updated_at": _now(),
        "cloned_from": cloned_from,
        "conversation_id": secrets.token_hex(8),
        "purpose": "",
        "change_log": [],
        "edit_requests": [],
    })
    folder = _entry_dir(prompt_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "prompt.txt").write_text(prompt or "", encoding="utf-8")
    (folder / "input_template.txt").write_text(input_template or "", encoding="utf-8")
    _write_json(_meta_path(prompt_id), meta)
    return _public_card(meta, owner)


def update_prompt(
    prompt_id: str,
    username: str,
    prompt: Optional[str] = None,
    input_template: Optional[str] = None,
    name: Optional[str] = None,
    content_column: Optional[str] = None,
) -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    if not meta:
        raise FileNotFoundError("Prompt not found.")
    if not can_edit(meta, username):
        raise PermissionError("You cannot edit this prompt.")
    body = load_body(prompt_id)
    before_prompt = body.get("prompt") or ""
    before_input = body.get("input_template") or ""
    if prompt is not None:
        body["prompt"] = prompt
    if input_template is not None:
        body["input_template"] = input_template
    if name is not None and name.strip():
        meta["name"] = name.strip()
    if content_column is not None:
        meta["content_column"] = content_column
    if (body.get("prompt") or "") != before_prompt or (body.get("input_template") or "") != before_input:
        _snapshot_version(prompt_id, meta, before_prompt, before_input, username)
    meta["required_inputs"] = required_placeholders(body["prompt"], body["input_template"])
    meta["updated_at"] = _now()
    folder = _entry_dir(prompt_id)
    (folder / "prompt.txt").write_text(body["prompt"] or "", encoding="utf-8")
    (folder / "input_template.txt").write_text(body["input_template"] or "", encoding="utf-8")
    _write_json(_meta_path(prompt_id), meta)
    return {**_public_card(meta, username), **body}


def update_acl(
    prompt_id: str,
    username: str,
    visibility: Optional[str] = None,
    everyone_role: Optional[str] = None,
    viewers: Optional[List[str]] = None,
    editors: Optional[List[str]] = None,
) -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    if not meta:
        raise FileNotFoundError("Prompt not found.")
    if not can_acl(meta, username):
        raise PermissionError("Only the owner can change who sees this prompt.")
    if visibility is not None:
        meta["visibility"] = visibility
    if everyone_role is not None:
        meta["everyone_role"] = everyone_role
    if viewers is not None:
        meta["viewers"] = viewers
    if editors is not None:
        meta["editors"] = editors
    meta = _normalize_meta(meta)
    meta["updated_at"] = _now()
    _write_json(_meta_path(prompt_id), meta)
    return _public_card(meta, username)


def delete_prompt(prompt_id: str, username: str) -> None:
    meta = load_meta(prompt_id)
    if not meta:
        raise FileNotFoundError("Prompt not found.")
    if not can_delete(meta, username):
        raise PermissionError("Only the person who created this prompt can delete it.")
    conversation_id = meta.get("conversation_id") or prompt_id
    folder = _entry_dir(prompt_id)
    for child in folder.iterdir():
        if child.is_file():
            child.unlink()
    try:
        folder.rmdir()
    except OSError:
        pass
    # The chat lives outside the prompt folder, so deleting the prompt used to
    # leave the conversation -- and whatever business context was typed into
    # it -- readable on disk forever.
    if not any(
        (other.get("conversation_id") or other.get("id")) == conversation_id for other in _iter_metas()
    ):
        try:
            _conversation_path(conversation_id).unlink(missing_ok=True)
        except Exception:
            pass


def clone_prompt(prompt_id: str, username: str) -> Dict[str, Any]:
    """Copy a prompt into the cloner's own space, chat history included.

    The clone gets its own conversation from the start. Sharing one conversation
    id between source and copy meant the first edit to either side silently
    emptied the other side's chat -- so cloning someone else's prompt and
    tweaking it destroyed their history, in their account, with no warning.
    """
    src = get_visible(prompt_id, username)
    src_meta = load_meta(prompt_id) or {}
    card = create_prompt(
        username,
        f"{src['name']} copy",
        src.get("prompt") or "",
        src.get("input_template") or "",
        visibility="personal",
        content_column=src.get("content_column") or "",
        cloned_from=prompt_id,
    )
    clone_meta = load_meta(card["id"])
    if clone_meta:
        clone_meta["purpose"] = src_meta.get("purpose") or ""
        clone_meta["change_log"] = list(src_meta.get("change_log") or [])
        _write_json(_meta_path(card["id"]), _normalize_meta(clone_meta))
        _write_json(_conversation_path(clone_meta["conversation_id"]), load_chat(prompt_id))
    return _public_card(load_meta(card["id"]) or clone_meta or {}, username)


def publish_version(
    source_id: str,
    username: str,
    version_name: str,
    prompt: str,
    input_template: Optional[str] = None,
    summary: str = "",
    purpose: str = "",
) -> Dict[str, Any]:
    """Make an accepted prompt fix loadable by the people who run the prompt.

    The fixer used to write only prompts/<name>.v2.txt. The New-run picker and
    the catalogue both read the catalogue, and the one-time legacy import has
    already run, so those versions existed on disk and appeared nowhere in the
    product. Editors update the prompt in place (the old text is snapshotted);
    everyone else gets their own copy carrying the same access rules.
    """
    src_meta = load_meta(source_id) if source_id else None
    body = load_body(source_id) if src_meta else {"prompt": "", "input_template": ""}
    extra = input_template if input_template is not None else body.get("input_template") or ""

    if src_meta and can_edit(src_meta, username):
        card = update_prompt(source_id, username, prompt=prompt, input_template=extra)
        if summary.strip():
            append_change(source_id, username, summary.strip(), purpose)
        return {**card, "published_to": source_id, "mode": "updated"}

    card = create_prompt(
        username,
        version_name,
        prompt,
        extra,
        visibility=(src_meta or {}).get("visibility") or "personal",
        everyone_role=(src_meta or {}).get("everyone_role") or "viewer",
        viewers=list((src_meta or {}).get("viewers") or []),
        editors=list((src_meta or {}).get("editors") or []),
        content_column=(src_meta or {}).get("content_column") or "",
        cloned_from=source_id or "",
    )
    if summary.strip():
        append_change(card["id"], username, summary.strip(), purpose)
    return {**load_and_card(card["id"], username), "published_to": card["id"], "mode": "created"}


def load_and_card(prompt_id: str, username: str) -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    return _public_card(meta, username) if meta else {}


def _conversations_dir() -> Path:
    path = CATALOGUE_DIR / "_conversations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _conversation_path(conversation_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "", conversation_id or "")
    if not safe:
        raise ValueError("Invalid conversation id.")
    return _conversations_dir() / f"{safe}.json"


def _conversation_id_for(prompt_id: str) -> str:
    meta = load_meta(prompt_id)
    if meta and meta.get("conversation_id"):
        return str(meta["conversation_id"])
    return prompt_id


def _migrate_legacy_chat(prompt_id: str, conversation_id: str) -> None:
    dest = _conversation_path(conversation_id)
    if dest.is_file():
        return
    legacy = _entry_dir(prompt_id) / "chat.json"
    if legacy.is_file():
        data = _read_json(legacy, [])
        _write_json(dest, data if isinstance(data, list) else [])


def load_chat(prompt_id: str) -> List[Dict[str, str]]:
    conversation_id = _conversation_id_for(prompt_id)
    _migrate_legacy_chat(prompt_id, conversation_id)
    data = _read_json(_conversation_path(conversation_id), [])
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, dict) and item.get("role") in ("user", "assistant"):
            out.append({"role": item["role"], "text": str(item.get("text") or "")})
    return out[-40:]


def append_chat(prompt_id: str, role: str, text: str) -> List[Dict[str, str]]:
    history = load_chat(prompt_id)
    history.append({"role": role, "text": text})
    _write_json(_conversation_path(_conversation_id_for(prompt_id)), history[-40:])
    return history


MAX_VERSIONS = 40


def _versions_path(prompt_id: str) -> Path:
    return _entry_dir(prompt_id) / "versions.json"


def _snapshot_version(
    prompt_id: str,
    meta: Dict[str, Any],
    old_prompt: str,
    old_input: str,
    by: str,
) -> None:
    """Keep the text a shared prompt had before this edit.

    A prompt with `everyone / editor` access can be overwritten by anyone
    logged in, and the change log only ever stored a one-line summary -- so a
    bad edit to a prompt other people run every week was unrecoverable.
    """
    history = _read_json(_versions_path(prompt_id), [])
    if not isinstance(history, list):
        history = []
    history.append({
        "at": _now(),
        "by": (by or "").strip(),
        "name": str(meta.get("name") or ""),
        "prompt": old_prompt or "",
        "input_template": old_input or "",
    })
    _write_json(_versions_path(prompt_id), history[-MAX_VERSIONS:])


def list_versions(prompt_id: str, username: str) -> List[Dict[str, Any]]:
    meta = load_meta(prompt_id)
    if not meta or not can_view(meta, username):
        raise PermissionError("You cannot see this prompt.")
    history = _read_json(_versions_path(prompt_id), [])
    return history if isinstance(history, list) else []


def restore_version(prompt_id: str, username: str, index: int) -> Dict[str, Any]:
    """Put a previous text back. The current text is snapshotted first."""
    history = list_versions(prompt_id, username)
    if index < 0 or index >= len(history):
        raise ValueError("That version no longer exists.")
    entry = history[index]
    return update_prompt(
        prompt_id,
        username,
        prompt=entry.get("prompt") or "",
        input_template=entry.get("input_template") or "",
    )


def request_edit(prompt_id: str, username: str) -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    if not meta:
        raise FileNotFoundError("Prompt not found.")
    if not can_view(meta, username):
        raise PermissionError("You cannot see this prompt.")
    if can_edit(meta, username):
        return _public_card(meta, username)
    user = (username or "").strip()
    requests = list(meta.get("edit_requests") or [])
    if user and user not in requests:
        requests.append(user)
        meta["edit_requests"] = requests
        meta["updated_at"] = _now()
        _write_json(_meta_path(prompt_id), _normalize_meta(meta))
    return _public_card(load_meta(prompt_id) or meta, username)


def resolve_edit_request(prompt_id: str, owner: str, username: str, grant: bool) -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    if not meta:
        raise FileNotFoundError("Prompt not found.")
    if not can_acl(meta, owner):
        raise PermissionError("Only the owner can grant edit access.")
    user = (username or "").strip()
    requests = [n for n in (meta.get("edit_requests") or []) if n != user]
    editors = list(meta.get("editors") or [])
    viewers = list(meta.get("viewers") or [])
    if grant and user and user != meta.get("owner"):
        if user not in editors:
            editors.append(user)
        viewers = [n for n in viewers if n != user]
    meta["edit_requests"] = requests
    meta["editors"] = editors
    meta["viewers"] = viewers
    meta["updated_at"] = _now()
    _write_json(_meta_path(prompt_id), _normalize_meta(meta))
    return _public_card(load_meta(prompt_id) or meta, owner)


def append_change(prompt_id: str, username: str, summary: str, purpose: str = "") -> Dict[str, Any]:
    meta = load_meta(prompt_id)
    if not meta:
        raise FileNotFoundError("Prompt not found.")
    text = (summary or "").strip()
    if not text:
        return _public_card(meta, username)
    if purpose.strip():
        meta["purpose"] = purpose.strip()
    log = list(meta.get("change_log") or [])
    log.append({
        "at": _now(),
        "by": (username or "").strip(),
        "summary": text,
        "purpose": meta.get("purpose") or "",
    })
    meta["change_log"] = _normalize_change_log(log)
    meta["updated_at"] = _now()
    _write_json(_meta_path(prompt_id), _normalize_meta(meta))
    return _public_card(load_meta(prompt_id) or meta, username)


def change_context(prompt_id: str) -> Dict[str, Any]:
    meta = load_meta(prompt_id) or {}
    return {
        "purpose": str(meta.get("purpose") or ""),
        "change_log": list(meta.get("change_log") or []),
    }


def migrate_legacy(username: str) -> None:
    """One-time: lift old prompts/*.txt into the catalogue as everyone/viewer."""
    marker = CATALOGUE_DIR / ".legacy_migrated"
    CATALOGUE_DIR.mkdir(parents=True, exist_ok=True)
    if marker.is_file() or not PROMPTS_DIR.is_dir():
        return
    owner = (username or "").strip() or "admin"
    for fname in os.listdir(PROMPTS_DIR):
        if not fname.endswith(".txt"):
            continue
        name = fname[:-4]
        text = (PROMPTS_DIR / fname).read_text(encoding="utf-8")
        create_prompt(owner, name, text, "", visibility="everyone", everyone_role="viewer")
    marker.write_text("1\n", encoding="utf-8")
