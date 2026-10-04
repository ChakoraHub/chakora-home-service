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

HOME_SERVICE_URL = os.getenv("HOME_SERVICE_URL", "http://localhost:5001")
MEETING_SERVICE_URL = os.getenv("MEETING_SERVICE_URL", "http://localhost:9000")
CHATBOT_SERVICE_URL = os.getenv("CHATBOT_SERVICE_URL", "http://localhost:7600")
ASSET_SERVICE_URL = os.getenv("ASSET_SERVICE_URL", "http://localhost:8090")
INTERNSHIP_SERVICE_URL = os.getenv("INTERNSHIP_SERVICE_URL", "http://localhost:5050")
MS365_SERVICE_URL = os.getenv("MS365_SERVICE_URL", "http://localhost:7700")
EMPLOYEE_SERVICE_URL = os.getenv("EMPLOYEE_SERVICE_URL", "http://localhost:8002")
BLOGGER_SERVICE_URL = os.getenv("BLOGGER_SERVICE_URL", "http://localhost:7500")
BRS_SERVICE_URL = os.getenv("BRS_SERVICE_URL", "http://localhost:8020")
LAMBDA_URL = 'https://lwug4xhfz27whiuu3acjfwsgtm0ttwja.lambda-url.eu-north-1.on.aws/'
STATIC_CDN = "https://d1pjjckqswt5z7.cloudfront.net"

CANONICAL_HOST = os.getenv("CANONICAL_HOST", "www.chakorahub.com").strip().lower()
INTERNSHIP_PUBLIC_HOST = os.getenv("INTERNSHIP_PUBLIC_HOST", "api.chakorahub.com").strip().lower()


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


# ── Secure PIN (mobile device login) request models ──────────────────────────

class PinSetupRequest(BaseModel):
    user_id: int
    device_id: str
    device_name: Optional[str] = None
    platform: Optional[str] = None
    pin: str


class PinLoginRequest(BaseModel):
    user_id: int
    device_id: str
    pin: str


class PinResetRequest(BaseModel):
    username: str          # email or username — verified like /home/login
    password: str
    device_id: str
    new_pin: str


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
# Cache helpers
# -----------------------
def _cache_backend_request(method: str, path: str, payload: Optional[dict] = None):
    """Cache backend is intentionally decoupled; fallback to DB/non-cache path."""
    _ = (method, path, payload)
    return None


# ── Home-section cache (DB 5 via /home/cache/*) ──────────────────────────────

def home_cache_get(section: str):
    try:
        encoded = urllib.parse.quote(section, safe="")
        response = _cache_backend_request("GET", f"/home/cache/get?section={encoded}")
        if response and response.get("success") and response.get("found"):
            print(f"✅ Home cache HIT: home:{section}")
            return response.get("data")
    except Exception as e:
        print(f"Home cache GET error [{section}]: {e}")
    return None


def home_cache_set(section: str, data, ttl: Optional[int] = None):
    try:
        payload = {"section": section, "data": data}
        if ttl:
            payload["ttl"] = ttl
        response = _cache_backend_request("POST", "/home/cache/set", payload=payload)
        if response and response.get("success"):
            print(f"✅ Home cache SET: home:{section} ttl={response.get('ttl')}s")
    except Exception as e:
        print(f"Home cache SET error [{section}]: {e}")


def home_cache_delete(section: str):
    try:
        encoded = urllib.parse.quote(section, safe="")
        response = _cache_backend_request("DELETE", f"/home/cache/delete?section={encoded}")
        if response and response.get("success"):
            print(f"🗑️ Home cache DELETE: home:{section}")
    except Exception as e:
        print(f"Home cache DELETE error [{section}]: {e}")


# ── Session cache (DB 0 via /session/*) ──────────────────────────────────────

def _session_set(user_id: int, value, ttl: int = 86400):
    try:
        response = _cache_backend_request(
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
        response = _cache_backend_request("DELETE", f"/session/delete?user_id={encoded_user_id}")
        if response and response.get("success"):
            print(f"🗑️ session DELETE: session:{user_id}")
    except Exception as e:
        print(f"session DELETE error [session:{user_id}]: {e}")


def _profile_set(user_id: int, value, ttl: int = 1800):
    try:
        response = _cache_backend_request(
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
        response = _cache_backend_request("DELETE", f"/profile/delete?user_id={encoded_user_id}")
        if response and response.get("success"):
            print(f"🗑️ profile DELETE: user:{user_id}")
    except Exception as e:
        print(f"profile DELETE error [user:{user_id}]: {e}")


def _auth_set(user_id: int, roles, usertype: str, ttl: int = 3600):
    try:
        safe_roles = roles if isinstance(roles, list) else []
        response = _cache_backend_request(
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
        response = _cache_backend_request("DELETE", f"/auth/delete?user_id={encoded_user_id}")
        if response and response.get("success"):
            print(f"🗑️ auth DELETE: roles:{user_id}")
    except Exception as e:
        print(f"auth DELETE error [roles:{user_id}]: {e}")


def _apicache_set(cache_key: str, value, ttl: int = 300):
    try:
        response = _cache_backend_request(
            "POST",
            "/apicache/set",
            payload={"cache_key": cache_key, "response": value, "ttl": ttl},
        )
        if response and response.get("success"):
            print(f"✅ apicache SET: {cache_key}")
    except Exception as e:
        print(f"apicache SET error [{cache_key}]: {e}")


def _apicache_delete(cache_key: str):
    try:
        encoded = urllib.parse.quote(cache_key, safe="")
        response = _cache_backend_request("DELETE", f"/apicache/delete?cache_key={encoded}")
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


# ==============================================================================
# ORACLE CONNECTION POOLING (Enterprise-grade reliability)
# ==============================================================================
_db_pool = None

def get_db_pool():
    global _db_pool
    if _db_pool is None:
        dsn = oracledb.makedsn(
            host=ORACLE_HOST,
            port=ORACLE_PORT,
            service_name=ORACLE_SERVICE_NAME,
        )
        _db_pool = oracledb.create_pool(
            user=ORACLE_USER,
            password=ORACLE_PASSWORD,
            dsn=dsn,
            min=2,
            max=10,
            increment=1,
            getmode=oracledb.POOL_GETMODE_WAIT,
            timeout=120,
            wait_timeout=15,
        )
        print("✅ Oracle DB connection pool initialized for CHAKORA schema")
    return _db_pool


def get_db_connection():
    """Acquires a pooled connection with auto-reconnect fallback."""
    try:
        try:
            pool = get_db_pool()
            conn = pool.acquire()
        except Exception as pool_err:
            print(f"⚠️ Pool acquire warning ({pool_err}), using direct connect fallback...")
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
                    print(f"⚠️ SES explicit credentials error ({explicit_exc}); trying default chain...")
                    ses = boto3.client("ses", region_name=aws_region)
                    _send_with_ses_client(ses)
                    return
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
    return {"status": "ok", "service": "home_service", "port": 5001}


# ==============================================================================
# AUTHENTICATION: LOGIN
# ==============================================================================
@app.post("/home/login")
async def home_login(
    request: Request,
    body: Optional[LoginRequest] = Body(default=None),
):
    """Login endpoint used by both web and mobile apps."""
    if body is not None:
        data = body.model_dump()
    else:
        data = await _get_request_data(request)

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
    # 1. User login (Students & General Users)
    # ------------------------------------------------------------------
    if login_type == "user":
        conn = get_db_connection()
        if not conn:
            print(f"❌ [home_login:user] db connection failed | username={username}")
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()
        user = None
        try:
            lock_col = _resolve_nrm_logins_lock_column(cursor)
            lock_select = f", l.{lock_col} AS ACCOUNT_LOCK_FLAG" if lock_col else ""

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
                print(f"❌ [home_login:user] empty password stored | user_id={user.get('ID')} username={username}")
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Login error: no password record found"},
                )

            password_format = "hashed" if db_password.startswith(("scrypt:", "pbkdf2:")) else "plain"
            try:
                if password_format == "hashed":
                    valid = check_password_hash(db_password, password)
                else:
                    valid = db_password == password
            except Exception as exc:
                print(f"❌ Password verification exception: {exc}")
                valid = False

            if not valid:
                print(f"⚠️ Invalid password for user {username}")
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Incorrect password"},
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
            print(f"✅ User login successful: {username}")

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
            _session_set(int(user["ID"]), profile, ttl=86400)
            _profile_set(int(user["ID"]), profile, ttl=1800)
            _auth_set(
                int(user["ID"]),
                [str((user.get("USERTYPE") or "user")).lower()],
                str((user.get("USERTYPE") or "user")).lower(),
                ttl=3600,
            )
            _apicache_set(f"home:user:{user['ID']}", profile, ttl=300)
        except Exception as e:
            print(f"Session proxy write warning: {e}")

        return {"success": True, "login_type": "user", "user": profile}

    # ------------------------------------------------------------------
    # 2. Employee login (Staff & Operations) - Case-Insensitive ID & Email
    # ------------------------------------------------------------------
    if login_type == "employee":
        employee_lookup = (employee_id or username).strip()
        conn = get_db_connection()
        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()
        try:
            # Case-insensitive comparison on both EMPLOYEE_ID and EMAIL
            cursor.execute("""
                SELECT
                    e.EMPLOYEE_ID,
                    e.EMPLOYEE_NAME,
                    e.EMAIL,
                    l.PASSWORD
                FROM EMP_NRM_EMPLOYEES e
                JOIN EMP_NRM_LOGINS l ON e.EMPLOYEE_ID = l.EMPLOYEE_ID
                WHERE UPPER(e.EMPLOYEE_ID) = UPPER(:1) OR LOWER(e.EMAIL) = LOWER(:2)
                ORDER BY e.EMPLOYEE_ID DESC
                FETCH FIRST 1 ROWS ONLY
            """, (employee_lookup, employee_lookup))

            _cols = [c[0] for c in cursor.description]
            _row = cursor.fetchone()
            emp = dict(zip(_cols, _row)) if _row else None

            if not emp:
                print(f"❌ Employee not found: {employee_lookup}")
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "message": "Employee not found"},
                )

            db_password = emp.get("PASSWORD") or ""
            if not db_password:
                return JSONResponse(
                    status_code=500,
                    content={"success": False, "message": "Login error: no password record found"},
                )

            pw_format = "hashed" if db_password.startswith(("scrypt:", "pbkdf2:")) else "plain"
            try:
                if pw_format == "hashed":
                    valid = check_password_hash(db_password, password)
                else:
                    valid = db_password == password
            except Exception as exc:
                print(f"❌ Employee password verification error: {exc}")
                valid = False

            if not valid:
                return JSONResponse(
                    status_code=401,
                    content={"success": False, "message": "Incorrect password"},
                )

            cursor.execute(
                """
                UPDATE EMP_NRM_LOGINS
                SET LOGOUT_TIME = NULL
                WHERE EMPLOYEE_ID = :1
                """,
                (emp["EMPLOYEE_ID"],),
            )
            conn.commit()
            print(f"✅ Employee login successful: {emp['EMPLOYEE_ID']}")

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
            traceback.print_exc()
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": f"Employee login failed: {e}"},
            )
        finally:
            try:
                cursor.close()
                conn.close()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════
# SECURE PIN — Mobile Device Login
# ══════════════════════════════════════════════════════════════════════════
PIN_MAX_FAILED_ATTEMPTS = int(os.getenv("PIN_MAX_FAILED_ATTEMPTS", "5"))


@app.post("/home/pin/setup")
async def pin_setup(payload: PinSetupRequest):
    pin = (payload.pin or "").strip()
    if not pin.isdigit() or len(pin) != 6:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "PIN must be exactly 6 digits"},
        )

    conn = get_db_connection()
    if not conn:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Database connection failed"},
        )

    cursor = conn.cursor()
    try:
        pin_hash = generate_password_hash(pin)
        cursor.execute(
            """
            MERGE INTO NRM_USER_DEVICE_PIN t
            USING (SELECT :user_id AS USER_ID, :device_id AS DEVICE_ID FROM dual) s
            ON (t.USER_ID = s.USER_ID AND t.DEVICE_ID = s.DEVICE_ID)
            WHEN MATCHED THEN UPDATE SET
                PIN_HASH        = :pin_hash,
                DEVICE_NAME     = :device_name,
                PLATFORM        = :platform,
                FAILED_ATTEMPTS = 0,
                IS_ACTIVE       = 'Y',
                UPDATED_AT      = CURRENT_TIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (USER_ID, DEVICE_ID, DEVICE_NAME, PLATFORM, PIN_HASH)
                VALUES (:user_id, :device_id, :device_name, :platform, :pin_hash)
            """,
            {
                "user_id": payload.user_id,
                "device_id": payload.device_id,
                "device_name": payload.device_name,
                "platform": payload.platform,
                "pin_hash": pin_hash,
            },
        )
        conn.commit()
        return {"success": True, "message": "Secure PIN set up successfully"}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Failed to set up Secure PIN"},
        )
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass


@app.post("/home/pin/login")
async def pin_login(payload: PinLoginRequest):
    pin = (payload.pin or "").strip()
    if not pin:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "PIN is required"},
        )

    conn = get_db_connection()
    if not conn:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Database connection failed"},
        )

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT ID, PIN_HASH, FAILED_ATTEMPTS, IS_ACTIVE
            FROM NRM_USER_DEVICE_PIN
            WHERE USER_ID = :1 AND DEVICE_ID = :2
            """,
            (payload.user_id, payload.device_id),
        )
        row = cursor.fetchone()

        if not row:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "No Secure PIN set up on this device"},
            )

        record_id, pin_hash, failed_attempts, is_active = row

        if str(is_active).strip().upper() != "Y":
            return JSONResponse(
                status_code=403,
                content={"success": False, "message": "Device is locked. Please log in with your password."},
            )

        valid = False
        try:
            valid = check_password_hash(pin_hash, pin)
        except Exception as exc:
            print(f"❌ PIN hash check error: {exc}")

        if not valid:
            new_failed = int(failed_attempts or 0) + 1
            lock_now = new_failed >= PIN_MAX_FAILED_ATTEMPTS
            cursor.execute(
                """
                UPDATE NRM_USER_DEVICE_PIN
                SET FAILED_ATTEMPTS = :1,
                    IS_ACTIVE = :2,
                    UPDATED_AT = CURRENT_TIMESTAMP
                WHERE ID = :3
                """,
                (new_failed, "N" if lock_now else "Y", record_id),
            )
            conn.commit()

            if lock_now:
                return JSONResponse(
                    status_code=403,
                    content={"success": False, "message": "Too many attempts. Device locked — please log in with your password."},
                )
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "Incorrect PIN"},
            )

        cursor.execute(
            """
            UPDATE NRM_USER_DEVICE_PIN
            SET FAILED_ATTEMPTS = 0,
                LAST_USED = CURRENT_TIMESTAMP,
                UPDATED_AT = CURRENT_TIMESTAMP
            WHERE ID = :1
            """,
            (record_id,),
        )

        cursor.execute(
            "SELECT ID, USERNAME, EMAIL, PHONE, USERTYPE, PROFILE_PIC FROM NRM_USERS WHERE ID = :1",
            (payload.user_id,),
        )
        _cols = [c[0] for c in cursor.description]
        _row = cursor.fetchone()
        user = dict(zip(_cols, _row)) if _row else None
        conn.commit()

        if not user:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "User not found"},
            )

        profile = {
            "id": user["ID"],
            "username": user.get("USERNAME"),
            "email": user.get("EMAIL"),
            "phone": user.get("PHONE"),
            "usertype": user.get("USERTYPE"),
            "profile_pic": user.get("PROFILE_PIC"),
        }
        try:
            _session_set(int(user["ID"]), profile, ttl=86400)
            _profile_set(int(user["ID"]), profile, ttl=1800)
            _auth_set(
                int(user["ID"]),
                [str((user.get("USERTYPE") or "user")).lower()],
                str((user.get("USERTYPE") or "user")).lower(),
                ttl=3600,
            )
        except Exception as e:
            print(f"Session proxy write error: {e}")

        return {"success": True, "login_type": "pin", "user": profile}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "PIN login failed"},
        )
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass


@app.post("/home/pin/reset")
async def pin_reset(payload: PinResetRequest):
    new_pin = (payload.new_pin or "").strip()
    if not new_pin.isdigit() or len(new_pin) != 6:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "PIN must be exactly 6 digits"},
        )

    conn = get_db_connection()
    if not conn:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Database connection failed"},
        )

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT u.ID, l.PASSWORD
            FROM NRM_USERS u
            JOIN NRM_LOGINS l ON u.ID = l.USER_ID
            WHERE LOWER(TRIM(u.EMAIL)) = LOWER(TRIM(:login_value))
               OR LOWER(TRIM(u.USERNAME)) = LOWER(TRIM(:login_value))
            ORDER BY l.CREATED_AT DESC
            FETCH FIRST 1 ROWS ONLY
            """,
            {"login_value": payload.username},
        )
        row = cursor.fetchone()
        if not row:
            return JSONResponse(
                status_code=404,
                content={"success": False, "message": "User not found"},
            )

        user_id, db_password = row
        db_password = db_password or ""
        password_format = "hashed" if db_password.startswith(("scrypt:", "pbkdf2:")) else "plain"
        try:
            valid = (
                check_password_hash(db_password, payload.password)
                if password_format == "hashed"
                else db_password == payload.password
            )
        except Exception:
            valid = False

        if not valid:
            return JSONResponse(
                status_code=401,
                content={"success": False, "message": "Invalid credentials"},
            )

        pin_hash = generate_password_hash(new_pin)
        cursor.execute(
            """
            MERGE INTO NRM_USER_DEVICE_PIN t
            USING (SELECT :user_id AS USER_ID, :device_id AS DEVICE_ID FROM dual) s
            ON (t.USER_ID = s.USER_ID AND t.DEVICE_ID = s.DEVICE_ID)
            WHEN MATCHED THEN UPDATE SET
                PIN_HASH = :pin_hash,
                FAILED_ATTEMPTS = 0,
                IS_ACTIVE = 'Y',
                UPDATED_AT = CURRENT_TIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                (USER_ID, DEVICE_ID, PIN_HASH)
                VALUES (:user_id, :device_id, :pin_hash)
            """,
            {"user_id": user_id, "device_id": payload.device_id, "pin_hash": pin_hash},
        )
        conn.commit()
        return {"success": True, "message": "Secure PIN reset successfully"}

    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Failed to reset Secure PIN"},
        )
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass


@app.get("/home/pin/status")
async def pin_status(user_id: int, device_id: str):
    conn = get_db_connection()
    if not conn:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Database connection failed"},
        )

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT 1 FROM NRM_USER_DEVICE_PIN
            WHERE USER_ID = :1 AND DEVICE_ID = :2 AND IS_ACTIVE = 'Y'
            """,
            (user_id, device_id),
        )
        exists = cursor.fetchone() is not None
        return {"success": True, "pin_exists": exists}
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Failed to check PIN status"},
        )
    finally:
        try:
            cursor.close()
            conn.close()
        except Exception:
            pass


# ==============================================================================
# FORGOT & RESET PASSWORD
# ==============================================================================
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

        return {"success": True, "message": "Reset link sent to mail"}
    except Exception as exc:
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


# ==============================================================================
# LOGOUT ROUTES
# ==============================================================================
@app.post("/home/logout/user")
async def user_logout(request: Request):
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

        try:
            _session_delete(int(user_id))
        except Exception:
            pass

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": "Logout successful",
            "user_id": user_id,
            "rows_updated": rows_updated,
        }

    except Exception as e:
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

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": "Logout successful",
            "employee_id": employee_id,
        }

    except Exception as e:
        if conn:
            conn.rollback()
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ==============================================================================
# CONTENT ENDPOINTS: BATCHES, FEEDBACK, ENQUIRIES, BLOGS, CLIENTS
# ==============================================================================
@app.get("/home/active-users")
async def get_active_users():
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse(
                status_code=500,
                content={"success": False, "message": "Database connection failed"},
            )

        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as active_users FROM NRM_LOGINS WHERE IS_ACTIVE = 'Y'")
        user_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) as active_employees FROM EMP_NRM_LOGINS WHERE LOGOUT_TIME IS NULL")
        employee_count = cursor.fetchone()[0]

        cursor.close()
        conn.close()

        return {
            "success": True,
            "active_user_count": user_count,
            "active_employee_count": employee_count,
            "total_active": user_count + employee_count,
        }

    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@app.get("/home/batches")
async def get_batches():
    cached = home_cache_get("batches")
    if cached:
        return cached

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

        home_cache_set("batches", response_data)
        return response_data

    except Exception as e:
        print("Batch API Error:", e)
        return {"success": False, "current_batches": [], "upcoming_batches": []}

    finally:
        cursor.close()
        conn.close()


@app.get("/home/feedbacks")
async def get_feedback():
    """Returns recent student feedback (Fixed for Oracle DB NULL semantics)."""
    cached_data = home_cache_get("feedback")
    if cached_data:
        return cached_data

    conn = get_db_connection()
    if not conn:
        return {"success": False, "feedbacks": []}

    cursor = conn.cursor()
    try:
        # Fixed: TRIM(f.FEEDBACK_MESSAGE) IS NOT NULL ensures rows with text are returned in Oracle
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
            LEFT JOIN "CHAKORA"."NRM_USERS" u ON f.STUDENT_ID = u.ID
            WHERE f.FEEDBACK_MESSAGE IS NOT NULL
              AND TRIM(f.FEEDBACK_MESSAGE) IS NOT NULL
            ORDER BY f.SUBMITTED_AT DESC
            FETCH FIRST 20 ROWS ONLY
            """
        )

        rows = cursor.fetchall()
        feedbacks = [{"username": row[0], "feedback_message": row[1]} for row in rows]
        response_data = {"success": True, "feedbacks": feedbacks}
        home_cache_set("feedback", response_data)
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
    cache_key = "about"
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
    home_cache_set(cache_key, response_data)
    return response_data


@app.get("/home/blogs")
async def get_blog_items():
    return {
        "success": True,
        "posts": [
            {
                "title": "Getting Started with Flutter",
                "summary": "A beginner-friendly walkthrough of widgets and state.",
                "date": "Aug 2026",
                "section": "Latest Articles",
            },
            {
                "title": "New Batch Starting Soon",
                "summary": "Registrations are now open for upcoming batches.",
                "date": "Aug 2026",
                "section": "Announcements",
            },
            {
                "title": "Data + Cloud Learning Trends",
                "summary": "What students should focus on in the current market.",
                "date": "Jul 2026",
                "section": "Technology News",
            },
        ],
    }


@app.get("/home/clients")
async def get_clients_items():
    return {
        "success": True,
        "clients": ["Acme Corp", "Globex Inc", "Initech"],
        "projects": ["Student Portal Revamp", "Internal Analytics Dashboard"],
        "testimonials": [
            {"name": "Priya S.", "quote": "The training program helped me land my first job."},
            {"name": "Arjun K.", "quote": "Hands-on projects made all the difference."},
        ],
    }


@app.delete("/home/cache/invalidate")
async def invalidate_home_cache(section: str = "batches"):
    home_cache_delete(section)
    label = f"home:{section}" if section != "*" else "home:*"
    return {"success": True, "message": f"Cache invalidated: {label}"}


# ==============================================================================
# ENTRYPOINT (Port 5001)
# ==============================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5001)
