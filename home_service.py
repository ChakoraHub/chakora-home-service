from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.responses import JSONResponse
import oracledb
import traceback
from werkzeug.security import check_password_hash, generate_password_hash
import json
import time
import uvicorn
from pydantic import BaseModel
from typing import Optional
import os
import pathlib
import smtplib
import urllib.parse
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
try:
    import boto3
except Exception:
    boto3 = None
try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if load_dotenv:
    load_dotenv(dotenv_path=pathlib.Path(__file__).resolve().parent / ".env", override=False)
else:
    print("⚠️ python-dotenv is not installed; continuing with process environment variables")


# ================= SERVICE URLS =================

HOME_SERVICE_URL = os.getenv("HOME_SERVICE_URL","http://localhost:5001")
MEETING_SERVICE_URL = os.getenv("MEETING_SERVICE_URL","http://localhost:9000")
CHATBOT_SERVICE_URL = os.getenv("CHATBOT_SERVICE_URL","http://localhost:7600")
ASSET_SERVICE_URL = os.getenv("ASSET_SERVICE_URL","http://localhost:8090")
INTERNSHIP_SERVICE_URL = os.getenv("INTERNSHIP_SERVICE_URL","http://localhost:5050")
MS365_SERVICE_URL = os.getenv("MS365_SERVICE_URL","http://localhost:7700")
EMPLOYEE_SERVICE_URL = os.getenv("EMPLOYEE_SERVICE_URL","http://localhost:8002")
BLOGGER_SERVICE_URL = os.getenv("BLOGGER_SERVICE_URL","http://localhost:7500")
REDIS_SERVICE_URL = os.getenv("REDIS_SERVICE_URL","http://localhost:6380")
BRS_SERVICE_URL = os.getenv("BRS_SERVICE_URL","http://localhost:8020")
LAMBDA_URL = 'https://lwug4xhfz27whiuu3acjfwsgtm0ttwja.lambda-url.eu-north-1.on.aws/'
STATIC_CDN = "https://d1pjjckqswt5z7.cloudfront.net"

CANONICAL_HOST = os.getenv("CANONICAL_HOST","www.chakorahub.com").strip().lower()
INTERNSHIP_PUBLIC_HOST = os.getenv("INTERNSHIP_PUBLIC_HOST","api.chakorahub.com").strip().lower()


def _clean_env_value(raw_value):
    if raw_value is None:
        return ""
    value = str(raw_value).strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1].strip()
    return value


def _get_env_value(name: str, default: str = "") -> str:
    value = _clean_env_value(os.getenv(name))
    return value or default

app = FastAPI(title="home_service", version="1.0")


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Catch-all: return JSON with traceback so errors are visible in logs and callers."""
    tb = traceback.format_exc()
    print(f"💥 [global-exception] {type(exc).__name__}: {exc}\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": f"{type(exc).__name__}: {exc}"},
    )


class LoginRequest(BaseModel):
    username: str
    password: str
    login_type: str = "user"
    employee_id: str = ""


class ForgotPasswordRequest(BaseModel):
    login_type: str
    username: str
    reset_base_url: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    password: str

ORACLE_HOST = os.getenv("ORACLE_HOST", "56.228.73.210")
ORACLE_PORT = int(os.getenv("ORACLE_PORT", "1521"))
ORACLE_SERVICE_NAME = os.getenv("ORACLE_SERVICE_NAME", "FREEPDB1")
ORACLE_USER = os.getenv("ORACLE_USER", "SUPPORT")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "Welcome123")

RESET_TOKEN_MAX_AGE_SECONDS = int(os.getenv("RESET_TOKEN_MAX_AGE_SECONDS", "1800"))
RESET_TOKEN_SECRET = (
    os.getenv("HOME_RESET_TOKEN_SECRET")
    or os.getenv("APP_SECRET_KEY")
    or "temporary123"
)
reset_serializer = URLSafeTimedSerializer(RESET_TOKEN_SECRET)

# -----------------------
# Redis cache helpers
# -----------------------
# All home-page cache operations go through redis_service's /home/cache/* endpoints.
# These routes live in DB 5 (API Response Cache) and enforce the correct TTLs
# (5–15 min per section) without any session logic.
#
# Section → Redis key mapping (enforced by redis_service):
#   "batches"  → home:batches   (10 min)
#   "feedback" → home:feedback  (5 min)
#   "offers"   → home:offers    (15 min)
#   "about"    → home:about     (15 min)
#
# For non-home cache needs (e.g. session:user:<id>) the generic /apicache/*
# and /session/* endpoints on redis_service are used directly.
# -----------------------

REDIS_SERVICE_URL = _get_env_value("REDIS_SERVICE_URL", "http://127.0.0.1:6380")


def _redis_service_candidates() -> list[str]:
    base = (REDIS_SERVICE_URL or "http://127.0.0.1:6380").rstrip("/")
    candidates = [base]

    if base.endswith(":6380"):
        candidates.append(base[:-5] + ":6390")
    elif base.endswith(":6390"):
        candidates.append(base[:-5] + ":6380")
    else:
        candidates.extend(["http://127.0.0.1:6390", "http://127.0.0.1:6380"])

    seen = set()
    unique = []
    for url in candidates:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _redis_service_request(method: str, path: str, payload: Optional[dict] = None):
    """Proxy cache operations to redis_service microservice."""
    body = None
    headers = {"Content-Type": "application/json"}

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    last_error = None
    for base in _redis_service_candidates():
        url = f"{base}{path}"
        req = urllib.request.Request(url=url, data=body, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as http_err:
            try:
                detail = http_err.read().decode("utf-8")
            except Exception:
                detail = str(http_err)
            last_error = f"HTTP {http_err.code} {detail}"
            print(f"Redis service HTTP error [{method} {path}] @ {base}: {last_error}")
        except Exception as exc:
            last_error = str(exc)
            print(f"Redis service request failed [{method} {path}] @ {base}: {exc}")

    if last_error:
        print(f"Redis service request exhausted [{method} {path}] last_error={last_error}")
    return None


# ── Home-section cache (DB 5 via /home/cache/*) ──────────────────────────────

def home_cache_get(section: str):
    """
    Fetch a home-page section from the redis_service home cache.
    Returns the cached data dict on HIT, or None on MISS / error.
    section: "batches" | "feedback" | "offers" | "about"
    """
    try:
        encoded = urllib.parse.quote(section, safe="")
        response = _redis_service_request("GET", f"/home/cache/get?section={encoded}")
        if response and response.get("success") and response.get("found"):
            print(f"✅ Home cache HIT: home:{section}")
            return response.get("data")
    except Exception as e:
        print(f"Home cache GET error [{section}]: {e}")
    return None


def home_cache_set(section: str, data, ttl: Optional[int] = None):
    """
    Store a home-page section in the redis_service home cache.
    ttl is optional — redis_service will use the section default if omitted.
    section: "batches" | "feedback" | "offers" | "about"
    """
    try:
        payload = {"section": section, "data": data}
        if ttl:
            payload["ttl"] = ttl
        response = _redis_service_request("POST", "/home/cache/set", payload=payload)
        if response and response.get("success"):
            print(f"✅ Home cache SET: home:{section} ttl={response.get('ttl')}s")
    except Exception as e:
        print(f"Home cache SET error [{section}]: {e}")


def home_cache_delete(section: str):
    """
    Invalidate a home-page section in the redis_service cache.
    Pass section='*' to flush all home cache keys at once.
    section: "batches" | "feedback" | "offers" | "about" | "*"
    """
    try:
        encoded = urllib.parse.quote(section, safe="")
        response = _redis_service_request("DELETE", f"/home/cache/delete?section={encoded}")
        if response and response.get("success"):
            print(f"🗑️ Home cache DELETE: home:{section}")
    except Exception as e:
        print(f"Home cache DELETE error [{section}]: {e}")


# ── Session cache (DB 0 via /session/*) ──────────────────────────────────────
# Canonical key written by redis_service: session:{user_id}

def _session_set(user_id: int, value, ttl: int = 86400):
    try:
        response = _redis_service_request(
            "POST",
            "/session/set",
            payload={"user_id": int(user_id), "data": value, "ttl": int(ttl)},
        )
        if response and response.get("success"):
            print(f"✅ session SET: session:{user_id}")
    except Exception as e:
        print(f"session SET error [session:{user_id}]: {e}")


def _session_delete(user_id: int):
    try:
        encoded_user_id = urllib.parse.quote(str(int(user_id)), safe="")
        response = _redis_service_request("DELETE", f"/session/delete?user_id={encoded_user_id}")
        if response and response.get("success"):
            print(f"🗑️ session DELETE: session:{user_id}")
    except Exception as e:
        print(f"session DELETE error [session:{user_id}]: {e}")


def _profile_set(user_id: int, value, ttl: int = 1800):
    try:
        response = _redis_service_request(
            "POST",
            "/profile/set",
            payload={"user_id": int(user_id), "data": value, "ttl": int(ttl)},
        )
        if response and response.get("success"):
            print(f"✅ profile SET: user:{user_id}")
    except Exception as e:
        print(f"profile SET error [user:{user_id}]: {e}")


def _profile_delete(user_id: int):
    try:
        encoded_user_id = urllib.parse.quote(str(int(user_id)), safe="")
        response = _redis_service_request("DELETE", f"/profile/delete?user_id={encoded_user_id}")
        if response and response.get("success"):
            print(f"🗑️ profile DELETE: user:{user_id}")
    except Exception as e:
        print(f"profile DELETE error [user:{user_id}]: {e}")


def _auth_set(user_id: int, roles, usertype: str, ttl: int = 3600):
    try:
        safe_roles = roles if isinstance(roles, list) else []
        response = _redis_service_request(
            "POST",
            "/auth/set",
            payload={
                "user_id": int(user_id),
                "roles": safe_roles,
                "usertype": (usertype or "user"),
                "ttl": int(ttl),
            },
        )
        if response and response.get("success"):
            print(f"✅ auth SET: roles:{user_id}")
    except Exception as e:
        print(f"auth SET error [roles:{user_id}]: {e}")


def _auth_delete(user_id: int):
    try:
        encoded_user_id = urllib.parse.quote(str(int(user_id)), safe="")
        response = _redis_service_request("DELETE", f"/auth/delete?user_id={encoded_user_id}")
        if response and response.get("success"):
            print(f"🗑️ auth DELETE: roles:{user_id}")
    except Exception as e:
        print(f"auth DELETE error [roles:{user_id}]: {e}")

def _apicache_set(cache_key: str, value, ttl: int = 300):
    """Store any payload in DB 5 (API response cache) by raw cache_key."""
    try:
        response = _redis_service_request(
            "POST",
            "/apicache/set",
            payload={"cache_key": cache_key, "response": value, "ttl": ttl},
        )
        if response and response.get("success"):
            print(f"✅ apicache SET: {cache_key}")
    except Exception as e:
        print(f"apicache SET error [{cache_key}]: {e}")


def _apicache_delete(cache_key: str):
    """Invalidate an entry in DB 5 by raw cache_key."""
    try:
        encoded = urllib.parse.quote(cache_key, safe="")
        response = _redis_service_request("DELETE", f"/apicache/delete?cache_key={encoded}")
        if response and response.get("success"):
            print(f"🗑️ apicache DELETE: {cache_key}")
    except Exception as e:
        print(f"apicache DELETE error [{cache_key}]: {e}")


def _mask_email_hint(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "@" not in raw:
        return f"set(len={len(raw)})"
    local_part, domain = raw.split("@", 1)
    if not local_part:
        return f"*@{domain}"
    return f"{local_part[0]}***@{domain}"


def _get_reset_email_config_snapshot() -> dict:
    sender_candidates = {
        "RESET_EMAIL_SENDER": (os.getenv("RESET_EMAIL_SENDER") or "").strip(),
        "SES_SENDER": (os.getenv("SES_SENDER") or "").strip(),
        "SMTP_USER": (os.getenv("SMTP_USER") or "").strip(),
        "EMAIL_SENDER": (os.getenv("EMAIL_SENDER") or "").strip(),
        "FROM_EMAIL": (os.getenv("FROM_EMAIL") or "").strip(),
    }
    resolved_sender_env = ""
    resolved_sender_value = ""
    for key, value in sender_candidates.items():
        if value:
            resolved_sender_env = key
            resolved_sender_value = value
            break

    return {
        "resolved_sender_env": resolved_sender_env,
        "resolved_sender_hint": _mask_email_hint(resolved_sender_value),
        "sender_candidates_present": {key: bool(value) for key, value in sender_candidates.items()},
        "password_candidates_present": {
            "RESET_EMAIL_PASSWORD": bool((os.getenv("RESET_EMAIL_PASSWORD") or "").strip()),
            "SMTP_PASSWORD": bool((os.getenv("SMTP_PASSWORD") or "").strip()),
        },
        "aws_candidates_present": {
            "AWS_ACCESS_KEY": bool((os.getenv("AWS_ACCESS_KEY") or "").strip()),
            "AWS_ACCESS_KEY_ID": bool((os.getenv("AWS_ACCESS_KEY_ID") or "").strip()),
            "AWS_SECRET_KEY": bool((os.getenv("AWS_SECRET_KEY") or "").strip()),
            "AWS_SECRET_ACCESS_KEY": bool((os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()),
        },
        "smtp_host": (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip(),
        "smtp_port": int(os.getenv("SMTP_PORT") or "587"),
        "aws_region": (os.getenv("AWS_REGION") or os.getenv("SES_REGION") or "eu-north-1").strip(),
        "boto3_available": boto3 is not None,
    }


print(f"📧 Forgot-password email config snapshot: {_get_reset_email_config_snapshot()}")

def get_db_connection():
    try:
        dsn = oracledb.makedsn(
            host=ORACLE_HOST,
            port=ORACLE_PORT,
            service_name=ORACLE_SERVICE_NAME,
        )

        conn = oracledb.connect(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=dsn,
        )

        cursor = conn.cursor()
        cursor.execute("ALTER SESSION SET CURRENT_SCHEMA = CHAKORA")
        cursor.close()

        return conn

    except Exception as e:
        print("DB Connection Error:", e)
        traceback.print_exc()
        return None


# ------------------------------------------------------------------
# Account-lock helpers — work with any lock column name in NRM_LOGINS
# ------------------------------------------------------------------
LOGIN_LOCK_COLUMN_CANDIDATES = ("ACCOUNT_LOCKED", "IS_LOCKED", "IS_BLOCKED", "LOCKED")
_lock_column_cache = None
_emp_login_columns_cache = None


def _is_truthy_lock_flag(value):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in {"Y", "YES", "TRUE", "1", "T"}


def _resolve_nrm_logins_lock_column(cursor):
    """Inspect NRM_LOGINS once per process and cache the first supported lock column."""
    global _lock_column_cache
    if _lock_column_cache is not None:
        return _lock_column_cache or None
    try:
        cursor.execute("SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME = 'NRM_LOGINS'")
        rows = cursor.fetchall() or []
        column_names = {str((row[0] if row else "")).strip().upper() for row in rows}
        for candidate in LOGIN_LOCK_COLUMN_CANDIDATES:
            if candidate in column_names:
                _lock_column_cache = candidate
                return candidate
        _lock_column_cache = ""
    except Exception as exc:
        print(f"⚠️ Could not inspect NRM_LOGINS columns for lock support: {exc}")
        _lock_column_cache = ""
    return None


def _resolve_emp_nrm_logins_columns(cursor):
    """Inspect EMP_NRM_LOGINS once per process and cache available columns."""
    global _emp_login_columns_cache
    if _emp_login_columns_cache is not None:
        return _emp_login_columns_cache
    try:
        cursor.execute("SELECT COLUMN_NAME FROM USER_TAB_COLUMNS WHERE TABLE_NAME = 'EMP_NRM_LOGINS'")
        rows = cursor.fetchall() or []
        _emp_login_columns_cache = {str((row[0] if row else "")).strip().upper() for row in rows}
    except Exception as exc:
        print(f"⚠️ Could not inspect EMP_NRM_LOGINS columns: {exc}")
        _emp_login_columns_cache = set()
    return _emp_login_columns_cache


async def _get_request_data(request: Request) -> dict:
    """Read JSON or form payloads safely."""
    try:
        body = await request.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass

    try:
        form = await request.form()
        return dict(form)
    except Exception:
        return {}


def _send_reset_email(to_email: str, reset_link: str) -> None:
    config_snapshot = _get_reset_email_config_snapshot()
    subject = "Password Reset"
    body_text = "\n".join([
        "Click below to reset your password:",
        "",
        reset_link,
        "",
        f"Valid for {RESET_TOKEN_MAX_AGE_SECONDS // 60} minutes.",
    ])

    sender = (
        os.getenv("RESET_EMAIL_SENDER")
        or os.getenv("SES_SENDER")
        or os.getenv("SMTP_USER")
        or os.getenv("EMAIL_SENDER")
        or os.getenv("FROM_EMAIL")
        or os.getenv("ADMIN_EMAIL")
        or "admin@chakorahub.com"
        or ""
    ).strip()
    password = (
        os.getenv("RESET_EMAIL_PASSWORD")
        or os.getenv("SMTP_PASSWORD")
        or ""
    ).strip()
    smtp_host = (os.getenv("SMTP_HOST") or "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT") or "587")

    # Primary: SMTP
    if sender and password:
        print(
            "📧 Forgot-password SMTP attempt | "
            f"to={_mask_email_hint(to_email)} sender_env={config_snapshot['resolved_sender_env']!r} "
            f"sender_hint={config_snapshot['resolved_sender_hint']!r} "
            f"host={config_snapshot['smtp_host']} port={config_snapshot['smtp_port']}"
        )
        try:
            msg = MIMEMultipart()
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = to_email
            msg.attach(MIMEText(body_text))

            server = smtplib.SMTP(smtp_host, smtp_port)
            server.starttls()
            server.login(sender, password)
            server.send_message(msg)
            server.quit()
            return
        except Exception as smtp_exc:
            print(f"⚠️ SMTP send failed, trying SES fallback: {smtp_exc}")

    # Fallback: AWS SES (works with IAM role or env keys)
    if boto3 is not None:
        ses_sender = (
            os.getenv("RESET_EMAIL_SENDER")
            or os.getenv("SES_SENDER")
            or os.getenv("EMAIL_SENDER")
            or os.getenv("FROM_EMAIL")
            or os.getenv("ADMIN_EMAIL")
            or "admin@chakorahub.com"
            or sender
            or ""
        ).strip()
        if not ses_sender:
            print(f"❌ Forgot-password sender missing | config={config_snapshot}")
            raise RuntimeError("Email sender is not configured")

        try:
            print(
                "📧 Forgot-password SES attempt | "
                f"to={_mask_email_hint(to_email)} sender_hint={_mask_email_hint(ses_sender)!r} "
                f"aws_region={config_snapshot['aws_region']}"
            )
            aws_region = (os.getenv("AWS_REGION") or os.getenv("SES_REGION") or "eu-north-1").strip()
            aws_access_key = (
                os.getenv("AWS_ACCESS_KEY")
                or os.getenv("AWS_ACCESS_KEY_ID")
                or ""
            ).strip()
            aws_secret_key = (
                os.getenv("AWS_SECRET_KEY")
                or os.getenv("AWS_SECRET_ACCESS_KEY")
                or ""
            ).strip()

            def _send_with_ses_client(ses_client):
                ses_client.send_email(
                    Source=ses_sender,
                    Destination={"ToAddresses": [to_email]},
                    Message={
                        "Subject": {"Data": subject},
                        "Body": {"Text": {"Data": body_text}},
                    },
                )

            if aws_access_key and aws_secret_key:
                try:
                    ses = boto3.client(
                        "ses",
                        aws_access_key_id=aws_access_key,
                        aws_secret_access_key=aws_secret_key,
                        region_name=aws_region,
                    )
                    _send_with_ses_client(ses)
                    return
                except Exception as explicit_exc:
                    explicit_error = str(explicit_exc)
                    retryable_auth_errors = (
                        "InvalidClientTokenId",
                        "SignatureDoesNotMatch",
                        "UnrecognizedClientException",
                        "security token included in the request is invalid",
                    )
                    if any(token in explicit_error for token in retryable_auth_errors):
                        print(
                            "⚠️ SES explicit credentials rejected; retrying with default credential chain "
                            f"(region={aws_region})"
                        )
                        ses = boto3.client("ses", region_name=aws_region)
                        _send_with_ses_client(ses)
                        return
                    raise
            else:
                ses = boto3.client("ses", region_name=aws_region)
                _send_with_ses_client(ses)
            return
        except Exception as ses_exc:
            raise RuntimeError(f"SES send failed: {ses_exc}")

    raise RuntimeError("No email provider configured (SMTP/SES)")


def _resolve_reset_base_url(base_url_from_client: Optional[str]) -> str:
    base = (base_url_from_client or "").strip()
    if base:
        return base.rstrip("/")
    return (os.getenv("WEB_PUBLIC_BASE_URL") or "https://www.chakorahub.com").rstrip("/")


@app.get("/health")
async def health_check():
    """Health endpoint for load balancers / simple checks."""
    return {"status": "ok", "service": "home_service"}


@app.post("/home/login")
async def home_login(
    request: Request,
    body: Optional[LoginRequest] = Body(default=None),
):
    """Login endpoint used by both web and mobile apps."""

    print(
        "🔐 [home_login] request received | "
        f"has_body={body is not None} method={request.method}"
    )

    if body is not None:
        data = body.model_dump()
    else:
        # Backward compatibility: accept form-encoded payloads from older clients.
        data = await _get_request_data(request)

    print(
        "🔐 [home_login] payload snapshot | "
        f"keys={sorted(list(data.keys())) if isinstance(data, dict) else type(data).__name__}"
    )

    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    login_type = (data.get("login_type") or "user").strip().lower()
    employee_id = (data.get("employee_id") or username).strip()

    if not username or not password:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "username/password required"},
        )

    # ------------------------------------------------------------------
    # User login
    # ------------------------------------------------------------------
    if login_type == "user":
        conn = get_db_connection()
        if not conn:
            print(f"❌ [home_login:user] db connection failed | username={username}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "db connection failed"},
            )

        print(f"🔎 [home_login:user] db connection ok | username={username}")
        cursor = conn.cursor()
        user = None
        try:
            lock_col = _resolve_nrm_logins_lock_column(cursor)
            lock_select = f", l.{lock_col} AS ACCOUNT_LOCK_FLAG" if lock_col else ""

            print(
                "🔎 [home_login:user] executing lookup | "
                f"username={username} login_type={login_type} lock_col={lock_col}"
            )

            cursor.execute(
                f"""
                SELECT
                    u.ID,
                    u.USERNAME,
                    u.EMAIL,
                    u.PHONE,
                    u.USERTYPE,
                    u.PROFILE_PIC,
                    l.CREATED_AT AS LOGIN_ROW_CREATED_AT,
                    l.UPDATED_AT AS LOGIN_ROW_UPDATED_AT,
                    l.IS_ACTIVE AS LOGIN_ROW_IS_ACTIVE,
                    l.PASSWORD
                    {lock_select}
                FROM NRM_USERS u
                JOIN NRM_LOGINS l ON u.ID = l.USER_ID
                WHERE LOWER(TRIM(u.EMAIL)) = LOWER(TRIM(:login_value))
                   OR LOWER(TRIM(u.USERNAME)) = LOWER(TRIM(:login_value))
                   OR u.PHONE = :phone_value
                ORDER BY l.CREATED_AT DESC
                FETCH FIRST 1 ROWS ONLY
                """,
                {"login_value": username, "phone_value": username},
            )
            _cols = [c[0] for c in cursor.description]
            _row = cursor.fetchone()
            user = dict(zip(_cols, _row)) if _row else None
            print(f"🔎 [home_login:user] lookup result | found={bool(user)}")

            if not user:
                print(f"❌ [home_login:user] user not found | username={username}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "message": "User not found"},
                )

            if lock_col and _is_truthy_lock_flag(user.get("ACCOUNT_LOCK_FLAG")):
                print(f"🔒 Locked account blocked at login: {username}")
                return JSONResponse(
                    status_code=403,
                    content={
                        "success": False,
                        "message": "Account is locked. Please contact support.",
                    },
                )

            db_password = user.get("PASSWORD") or ""
            if not db_password:
                print(f"❌ [home_login:user] empty password returned | user_id={user.get('ID')} username={username}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Login error"},
                )

            print(
                "🔎 Login row selected | "
                f"login_type=user user_id={user.get('ID')} username={username} "
                f"row_created_at={user.get('LOGIN_ROW_CREATED_AT')} "
                f"row_updated_at={user.get('LOGIN_ROW_UPDATED_AT')} "
                f"row_is_active={user.get('LOGIN_ROW_IS_ACTIVE')}"
            )

            password_format = "hashed" if db_password.startswith(("scrypt:", "pbkdf2:")) else "plain"
            hash_scheme = db_password.split(":", 1)[0] if password_format == "hashed" else "plain-text"
            db_has_outer_spaces = db_password != db_password.strip()

            try:
                if password_format == "hashed":
                    valid = check_password_hash(db_password, password)
                else:
                    valid = db_password == password
                print(
                    "🔎 [home_login:user] password check complete | "
                    f"user_id={user.get('ID')} valid={valid} scheme={hash_scheme}"
                )
            except Exception as exc:
                print(
                    "❌ Password verify error | "
                    f"login_type=user user_id={user.get('ID')} username={username} "
                    f"scheme={hash_scheme} db_len={len(db_password)} input_len={len(password)} "
                    f"db_outer_spaces={db_has_outer_spaces} error={exc}"
                )
                valid = False

            if not valid:
                print(
                    "⚠️ Invalid credentials | "
                    f"login_type=user user_id={user.get('ID')} username={username} "
                    f"row_created_at={user.get('LOGIN_ROW_CREATED_AT')} row_updated_at={user.get('LOGIN_ROW_UPDATED_AT')} "
                    f"scheme={hash_scheme} db_len={len(db_password)} input_len={len(password)} "
                    f"db_outer_spaces={db_has_outer_spaces} "
                    f"input_is_default_pw={password == 'changeme123'} "
                    f"db_hash_prefix={db_password[:20]!r}"
                )
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Invalid credentials"},
                )

            cursor.execute(
                """
                UPDATE NRM_LOGINS
                SET IS_ACTIVE = 'Y',
                    LAST_LOGIN = CURRENT_TIMESTAMP,
                    UPDATED_AT = CURRENT_TIMESTAMP
                WHERE USER_ID = :1
                """,
                (user["ID"],),
            )
            conn.commit()
            print(f"✅ User login successful, IS_ACTIVE=Y: {username}")

        finally:
            try:
                cursor.close()
                conn.close()
            except Exception:
                pass

        profile = {
            "id": user["ID"],
            "username": user.get("USERNAME"),
            "email": user.get("EMAIL"),
            "phone": user.get("PHONE"),
            "usertype": user.get("USERTYPE"),
            "profile_pic": user.get("PROFILE_PIC"),
        }
        try:
            # Store canonical session in DB 0 as session:{user_id}
            print(f"📦 [home_login:user] writing session/profile/auth caches | user_id={user['ID']}")
            _session_set(int(user["ID"]), profile, ttl=86400)
            # Store profile cache in DB 1 as user:{user_id}
            _profile_set(int(user["ID"]), profile, ttl=1800)
            # Store authorization cache in DB 2 as roles:{user_id}
            _auth_set(
                int(user["ID"]),
                [str((user.get("USERTYPE") or "user")).lower()],
                str((user.get("USERTYPE") or "user")).lower(),
                ttl=3600,
            )
            # Store login API response in DB 5
            _apicache_set(f"home:user:{user['ID']}", profile, ttl=300)
        except Exception as e:
            print(f"Redis session proxy write error: {e}")

        print(
            "✅ [home_login:user] response prepared | "
            f"user_id={user['ID']} email={user.get('EMAIL')} phone={user.get('PHONE')}"
        )

        return {"success": True, "login_type": "user", "user": profile}

    # ------------------------------------------------------------------
    # Employee login
    # ------------------------------------------------------------------
    if login_type == "employee":
        employee_lookup = employee_id or username
        print(f"🔐 [emp-login] START lookup={employee_lookup!r}")
        conn = get_db_connection()
        if not conn:
            print("❌ [emp-login] DB connection failed")
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "db connection failed"},
            )

        print("✅ [emp-login] DB connection OK")
        cursor = conn.cursor()
        try:
            print("🔎 [emp-login] Executing SELECT on EMP_NRM_EMPLOYEES JOIN EMP_NRM_LOGINS")
            cursor.execute("""
                SELECT
                    e.EMPLOYEE_ID,
                    e.EMPLOYEE_NAME,
                    e.EMAIL,
                    l.PASSWORD
                FROM EMP_NRM_EMPLOYEES e
                JOIN EMP_NRM_LOGINS l ON e.EMPLOYEE_ID = l.EMPLOYEE_ID
                WHERE e.EMPLOYEE_ID = :1 OR LOWER(e.EMAIL) = LOWER(:2)
                ORDER BY e.EMPLOYEE_ID DESC
                FETCH FIRST 1 ROWS ONLY
            """, (employee_lookup, employee_lookup))

            _cols = [c[0] for c in cursor.description]
            _row = cursor.fetchone()
            emp = dict(zip(_cols, _row)) if _row else None
            print(f"🔎 [emp-login] fetchone result: {'found' if emp else 'None'}")

            if not emp:
                print(f"❌ [emp-login] Employee not found: {employee_lookup!r}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "message": "Employee not found"},
                )

            db_password = emp.get("PASSWORD")
            print(f"🔎 [emp-login] emp keys={list(emp.keys())} password_present={'yes' if db_password else 'no'} password_len={len(db_password) if db_password else 0}")

            if not db_password:
                print("❌ [emp-login] No password stored for employee")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Login error"},
                )

            # ✅ Password check
            pw_format = "hashed" if db_password.startswith(("scrypt:", "pbkdf2:")) else "plain"
            print(f"🔎 [emp-login] pw_format={pw_format} db_pw_prefix={db_password[:20]!r}")
            try:
                if pw_format == "hashed":
                    valid = check_password_hash(db_password, password)
                else:
                    valid = db_password == password
                print(f"🔎 [emp-login] password_valid={valid}")
            except Exception as exc:
                print(f"❌ [emp-login] Password verification error: {exc}")
                traceback.print_exc()
                valid = False

            if not valid:
                print(f"❌ [emp-login] Invalid credentials for {employee_lookup!r} pw_format={pw_format}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Invalid credentials"},
                )

            # Mark active employee session using available schema.
            # EMP_NRM_LOGINS has no IS_ACTIVE/LAST_LOGIN/UPDATED_AT columns.
            print("🔎 [emp-login] Running UPDATE LOGOUT_TIME=NULL")
            cursor.execute(
                """
                UPDATE EMP_NRM_LOGINS
                SET LOGOUT_TIME = NULL
                WHERE EMPLOYEE_ID = :1
                """,
                (emp["EMPLOYEE_ID"],),
            )
            conn.commit()
            print(f"✅ [emp-login] Login success for employee_id={emp['EMPLOYEE_ID']}")

            # ✅ Success
            return {
                "success": True,
                "login_type": "employee",
                "employee": {
                    "employee_id": emp["EMPLOYEE_ID"],
                    "employee_name": emp["EMPLOYEE_NAME"],
                    "email": emp["EMAIL"]
                }
            }

        except Exception as e:
            print(f"❌ [emp-login] EXCEPTION: {type(e).__name__}: {e}")
            traceback.print_exc()
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": f"Employee login failed: {type(e).__name__}: {e}"},
            )
        finally:
            try:
                cursor.close()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass


@app.post("/home/forgot-password")
async def home_forgot_password(payload: ForgotPasswordRequest):
    login_type = (payload.login_type or "").strip().lower()
    username = (payload.username or "").strip()
    if login_type not in {"user", "employee"}:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid login type"},
        )
    if not username:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "username required"},
        )

    conn = get_db_connection()
    if not conn:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Database connection failed"},
        )

    cursor = None
    try:
        cursor = conn.cursor()
        if login_type == "user":
            cursor.execute(
                """
                SELECT EMAIL
                FROM NRM_USERS
                WHERE LOWER(EMAIL) = LOWER(:1)
                FETCH FIRST 1 ROWS ONLY
                """,
                (username,),
            )
        else:
            cursor.execute(
                """
                SELECT EMAIL
                FROM EMP_NRM_EMPLOYEES
                WHERE LOWER(EMAIL) = LOWER(:1)
                FETCH FIRST 1 ROWS ONLY
                """,
                (username,),
            )

        row = cursor.fetchone()
        if not row:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "User not found"},
            )

        email = row[0]
        token = reset_serializer.dumps({"email": email, "login_type": login_type})
        reset_base_url = _resolve_reset_base_url(payload.reset_base_url)
        link = f"{reset_base_url}/reset-password/{token}"

        _send_reset_email(email, link)

        return {
            "success": True,
            "message": "Reset link sent to mail",
        }
    except Exception as exc:
        print(
            "❌ Forgot password error | "
            f"login_type={login_type} username={username} error={exc}"
        )
        return JSONResponse(
            status_code=503,
            content={"success": False, "message": f"Unable to send reset email: {exc}"},
        )
    finally:
        try:
            if cursor:
                cursor.close()
            conn.close()
        except Exception:
            pass


@app.get("/home/reset-password/validate")
async def validate_reset_token(token: str):
    try:
        data = reset_serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE_SECONDS)
        return {
            "success": True,
            "email": data.get("email"),
            "login_type": data.get("login_type"),
        }
    except SignatureExpired:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Reset link expired"},
        )
    except BadSignature:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid reset link"},
        )
    except Exception as exc:
        print(f"❌ Reset token validation error: {exc}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Token validation failed"},
        )


@app.post("/home/reset-password/{token}")
async def home_reset_password(token: str, payload: ResetPasswordRequest):
    password = (payload.password or "").strip()
    if not password:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "password required"},
        )

    try:
        data = reset_serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Reset link expired"},
        )
    except BadSignature:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid reset link"},
        )

    email = (data.get("email") or "").strip()
    login_type = (data.get("login_type") or "").strip().lower()
    if login_type not in {"user", "employee"} or not email:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid reset payload"},
        )

    conn = get_db_connection()
    if not conn:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Database connection failed"},
        )

    cursor = None
    try:
        hashed = generate_password_hash(password)
        cursor = conn.cursor()

        if login_type == "user":
            cursor.execute(
                """
                UPDATE NRM_LOGINS
                SET PASSWORD = :1, UPDATED_AT = CURRENT_TIMESTAMP
                WHERE USER_ID = (
                    SELECT ID
                    FROM NRM_USERS
                    WHERE LOWER(EMAIL) = LOWER(:2)
                    ORDER BY ID DESC
                    FETCH FIRST 1 ROWS ONLY
                )
                """,
                (hashed, email),
            )
        else:
            cursor.execute(
                """
                UPDATE EMP_NRM_LOGINS
                SET PASSWORD = :1
                WHERE EMPLOYEE_ID = (
                    SELECT EMPLOYEE_ID
                    FROM EMP_NRM_EMPLOYEES
                    WHERE LOWER(EMAIL) = LOWER(:2)
                    FETCH FIRST 1 ROWS ONLY
                )
                """,
                (hashed, email),
            )

        rows_updated = cursor.rowcount
        conn.commit()

        if rows_updated <= 0:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "Account not found for reset"},
            )

        return {"success": True, "message": "Password reset successful"}
    except Exception as exc:
        print(f"❌ Reset password error: {exc}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Password reset failed"},
        )
    finally:
        try:
            if cursor:
                cursor.close()
            conn.close()
        except Exception:
            pass


# ==================== LOGOUT ROUTES ====================

@app.post("/home/logout/user")
async def user_logout(request: Request):
    """Handle user logout - set IS_ACTIVE='N' and record LOGOUT_TIME."""
    data = await _get_request_data(request)
    user_id = data.get("user_id")

    if not user_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "user_id required"},
        )

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE NRM_LOGINS
            SET IS_ACTIVE = 'N',
                LOGOUT_TIME = CURRENT_TIMESTAMP,
                UPDATED_AT = CURRENT_TIMESTAMP
            WHERE USER_ID = :1
            """,
            (user_id,),
        )

        rows_updated = cursor.rowcount
        conn.commit()

        if rows_updated <= 0:
            print(f"⚠️ Logout update matched no rows in NRM_LOGINS for USER_ID={user_id}")
            return JSONResponse(
                status_code=404,
                content={
                    "success": False,
                    "message": "No NRM_LOGINS row updated during logout",
                    "user_id": user_id,
                    "rows_updated": rows_updated,
                },
            )

        cursor.execute(
            """
            SELECT USER_ID, IS_ACTIVE, LAST_LOGIN, LOGOUT_TIME, UPDATED_AT
            FROM NRM_LOGINS
            WHERE USER_ID = :1
            ORDER BY UPDATED_AT DESC NULLS LAST
            FETCH FIRST 1 ROWS ONLY
            """,
            (user_id,),
        )
        _cols = [c[0] for c in cursor.description]
        _row = cursor.fetchone()
        updated_row = dict(zip(_cols, _row)) if _row else None

        if not updated_row or (updated_row.get("IS_ACTIVE") or "").upper() != "N":
            print(f"❌ Logout verification failed for USER_ID={user_id}: {updated_row}")
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "message": "Logout verification failed",
                    "user_id": user_id,
                    "row": updated_row,
                },
            )

        try:
            # Remove only canonical session from DB 0 on logout.
            # Keep profile/auth/api caches to speed up subsequent logins.
            _session_delete(int(user_id))
        except Exception as redis_exc:
            print(
                f"⚠️ Redis session delete proxy failed on logout for USER_ID={user_id}: {redis_exc}"
            )

        print(
            f"✅ User logout verified: USER_ID={user_id}, rows_updated={rows_updated}, "
            f"IS_ACTIVE={updated_row.get('IS_ACTIVE')}, LOGOUT_TIME={updated_row.get('LOGOUT_TIME')}"
        )

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": "Logout successful",
            "user_id": user_id,
            "rows_updated": rows_updated,
            "is_active": updated_row.get("IS_ACTIVE"),
            "logout_time": str(updated_row.get("LOGOUT_TIME")) if updated_row.get("LOGOUT_TIME") else None,
        }

    except Exception as e:
        print(f"❌ User logout error: {e}")
        if conn:
            conn.rollback()
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.post("/home/logout/employee")
async def employee_logout(request: Request):
    """Handle employee logout by recording LOGOUT_TIME for EMP_NRM_LOGINS."""
    data = await _get_request_data(request)
    employee_id = data.get("employee_id")

    if not employee_id:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "employee_id required"},
        )

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE EMP_NRM_LOGINS
            SET LOGOUT_TIME = CURRENT_TIMESTAMP
            WHERE EMPLOYEE_ID = :1
            """,
            (employee_id,),
        )

        rows_updated = cursor.rowcount
        conn.commit()

        print(f"✅ Employee logout successful: EMPLOYEE_ID={employee_id}, rows_updated={rows_updated}")

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": "Logout successful",
            "employee_id": employee_id,
        }

    except Exception as e:
        print(f"❌ Employee logout error: {e}")
        if conn:
            conn.rollback()
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/home/active-users")
async def get_active_users():
    """Get count of currently active users."""
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT COUNT(*) as active_users
            FROM NRM_LOGINS
            WHERE IS_ACTIVE = 'Y'
            """
        )
        user_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COUNT(*) as active_employees
            FROM EMP_NRM_LOGINS
            WHERE LOGOUT_TIME IS NULL
            """
        )
        employee_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT
                l.USER_ID,
                u.FULL_NAME,
                u.EMAIL,
                l.LAST_LOGIN,
                ROUND((CAST(SYSTIMESTAMP AS DATE) - CAST(l.LAST_LOGIN AS DATE)) * 24 * 60) as minutes_since_login
            FROM NRM_LOGINS l
            JOIN NRM_USERS u ON l.USER_ID = u.ID
            WHERE l.IS_ACTIVE = 'Y'
            ORDER BY l.LAST_LOGIN DESC
            """
        )
        active_users = []
        for row in cursor.fetchall():
            active_users.append(
                {
                    "user_id": row[0],
                    "name": row[1],
                    "email": row[2],
                    "last_login": str(row[3]) if row[3] else None,
                    "minutes_since_login": row[4],
                }
            )

        cursor.close()
        conn.close()

        return {
            "success": True,
            "active_user_count": user_count,
            "active_employee_count": employee_count,
            "total_active": user_count + employee_count,
            "active_users": active_users,
        }

    except Exception as e:
        print(f"❌ Get active users error: {e}")
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/home/batches")
async def get_batches():
    """
    Returns current and upcoming batches.
    Cache key : home:batches  (DB 5, TTL 10 min)
    Cache MISS → query Oracle → store in home cache → return.
    """
    start_time = time.time()

    cached = home_cache_get("batches")
    if cached:
        print(f"⏱️ batches served from cache ({time.time() - start_time:.3f}s)")
        return cached

    print("❄️ Cache MISS for home:batches — querying Oracle")
    conn = get_db_connection()
    if not conn:
        return {"success": False, "current_batches": [], "upcoming_batches": []}

    cursor = conn.cursor()
    current_batches = []
    upcoming_batches = []

    try:
        cursor.execute(
            """
            SELECT
                COALESCE(b.DISPLAY_NAME, c.COURSE_NAME) AS course_name,
                b.LANGUAGE                              AS language_name,
                b.START_DATE,
                b.END_DATE,
                b.BATCH_TYPE,
                b.NOTES,
                b.STATUS
            FROM CHAKORA.NRM_BATCH_SCHEDULE b
            LEFT JOIN CHAKORA.NRM_COURSES c ON b.COURSE_ID = c.ID
            WHERE LOWER(COALESCE(TRIM(b.STATUS), 'upcoming')) NOT IN ('deleted', 'completed')
            ORDER BY b.START_DATE ASC
            """
        )

        rows = cursor.fetchall()
        today = __import__("datetime").date.today()

        for row in rows:
            entry = {
                "course_name": row[0] or "TBD",
                "language_name": row[1] or "",
                "start_date": str(row[2]) if row[2] else "",
                "batch_type": row[4] or "regular",
                "notes": row[5] or "",
            }
            explicit_status = str(row[6] or "").strip().lower()
            start = row[2].date() if hasattr(row[2], "date") else row[2]
            end = row[3].date() if (row[3] and hasattr(row[3], "date")) else row[3]

            is_current = bool(start and start <= today and (not end or end >= today))
            is_upcoming = bool(start and start > today)

            if explicit_status == "current" or is_current:
                current_batches.append(entry)
            elif explicit_status == "upcoming" or is_upcoming:
                upcoming_batches.append(entry)

        response_data = {
            "success": True,
            "current_batches": current_batches,
            "upcoming_batches": upcoming_batches,
        }

        home_cache_set("batches", response_data)   # TTL = 10 min (redis_service default)
        print(f"⏱️ batches total time: {time.time() - start_time:.2f}s")
        return response_data

    except Exception as e:
        print("Batch API Error:", e)
        return {"success": False, "current_batches": [], "upcoming_batches": []}

    finally:
        cursor.close()
        conn.close()


@app.get("/home/feedbacks")
async def get_feedback():
    """
    Returns recent student feedback.
    Cache key : home:feedback  (DB 5, TTL 5 min)
    Cache MISS -> query Oracle -> store in home cache -> return.
    """
    start_time = time.time()

    cached_data = home_cache_get("feedback")
    if cached_data:
        print(f"[TIME]️ feedbacks served from cache ({time.time() - start_time:.3f}s)")
        return cached_data

    print("❄️ Cache MISS for home:feedback -- querying Oracle")

    conn = get_db_connection()
    if not conn:
        return {"success": False, "feedbacks": []}

    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT
                COALESCE(
                    NULLIF(f.NAME, ''),
                    NULLIF(u.USERNAME, ''),
                    'Anonymous'
                ) AS username,
                f.FEEDBACK_MESSAGE
                        FROM "CHAKORA"."NRM_FEEDBACK" f
                        LEFT JOIN "CHAKORA"."NRM_USERS" u
                ON f.STUDENT_ID = u.ID
            WHERE f.FEEDBACK_MESSAGE IS NOT NULL
              AND TRIM(f.FEEDBACK_MESSAGE) <> ''
            ORDER BY f.SUBMITTED_AT DESC
                        FETCH FIRST 20 ROWS ONLY
            """
        )

        rows = cursor.fetchall()

        feedbacks = []
        for row in rows:
            feedbacks.append({"username": row[0], "feedback_message": row[1]})

        response_data = {"success": True, "feedbacks": feedbacks}

        home_cache_set("feedback", response_data)  # TTL = 5 min (redis_service default)
        print(f"[TIME]️ feedbacks total time: {time.time() - start_time:.2f}s")
        return response_data

    except Exception as e:
        print("Feedback Error:", e)
        return {"success": False, "feedbacks": []}

    finally:
        cursor.close()
        conn.close()


@app.post("/home/enquiry")
async def enquiry(request: Request):
    cursor = None
    conn = None
    try:
        data = await _get_request_data(request)
        print("🏠 [HOME] enquiry:", data)

        if not data:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Invalid data"},
            )

        conn = get_db_connection()
        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()

        user_id = data.get("user_id")
        name = data.get("name")
        email = data.get("email")
        phone = data.get("phone")
        enquiry_text = data.get("enquiry") or data.get("enquiry_text")

        if not enquiry_text:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "Enquiry required"},
            )

        is_guest = True if not user_id else False

        cursor.execute(
            """
            INSERT INTO NRM_ENQUIRIES
            (STUDENT_ID, NAME, EMAIL, PHONE, ENQUIRY, CREATED_AT, IS_GUEST_ENQUIRY)
            VALUES (:1, :2, :3, :4, :5, CURRENT_TIMESTAMP, :6)
            """,
            (user_id, name, email, phone, enquiry_text, is_guest),
        )

        conn.commit()

        return {"success": True, "message": "Enquiry submitted successfully"}

    except Exception as e:
        print("❌ Enquiry error:", e)
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Server error"},
        )

    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass


@app.get("/home/gallery")
async def get_gallery_items():
    """
    Returns gallery items.
    Cache key : home:about  — gallery is part of the 'about' section family.
    TTL 15 min (rarely changes).
    """
    cache_key = "about"   # maps to home:about in redis_service

    cached_data = home_cache_get(cache_key)
    if cached_data:
        return cached_data

    response_data = {
        "success": True,
        "items": [
            {
                "title": "Certificate of Recognition",
                "category": "Certificate",
                "description": "Awarded to Subhash Chandra Vidapanakal for commitment to quality delivery at Kaiser Permanente.",
                "presented_at": "RFS-BOS All Hands | August 2018",
                "image_url": "/static/certificate.jpeg",
            }
        ],
    }

    home_cache_set(cache_key, response_data)  # TTL = 15 min (redis_service default)

    return response_data


# ── Cache management endpoints (admin / internal use) ────────────────────────

@app.delete("/home/cache/invalidate")
async def invalidate_home_cache(section: str = "batches"):
    """
    Admin endpoint to manually bust a home-page cache section.
    section: batches | feedback | offers | about | * (all)
    Example: DELETE /home/cache/invalidate?section=batches
    """
    home_cache_delete(section)
    label = f"home:{section}" if section != "*" else "home:*"
    return {"success": True, "message": f"Cache invalidated: {label}"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5001)
