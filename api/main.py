from fastapi import FastAPI, HTTPException, Depends, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer
from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime, timedelta, timezone
try:
    import oracledb as cx_Oracle
except ImportError:
    import cx_Oracle
import base64
import hashlib
import hmac
import logging
import os
import sys
import asyncio
from contextlib import contextmanager, asynccontextmanager
import json
from dotenv import load_dotenv
load_dotenv()

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from mqtt.mqtt_client import get_mqtt_singleton


logger = logging.getLogger(__name__)

clientMqqt = get_mqtt_singleton()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan context manager.
    Handles startup and shutdown events for MQTT connection.
    
    Startup:
    - Initialize MQTT singleton and wait for broker connection
    - Timeout is set to 10 seconds; publishes will use fallback if needed
    
    Shutdown:
    - Gracefully disconnect MQTT client
    """
    global clientMqqt
    
    # ============ STARTUP ============
    try:
        clientMqqt = get_mqtt_singleton()
        logger.info("MQTT singleton initialized during FastAPI startup")
        
        mqtt_ready = await asyncio.wait_for(
            clientMqqt.wait_for_connection(timeout=10),
            timeout=11  # Slightly higher than client timeout
        )
        if mqtt_ready:
            logger.info("✓ MQTT broker connection established")
        else:
            logger.warning("⚠ MQTT broker connection not established - publishes will use fallback mode")
    except asyncio.TimeoutError:
        logger.warning("⚠ MQTT broker connection timeout - publishes will use fallback mode")
    except Exception as e:
        logger.error(f"Error during MQTT connection wait: {e}")
    
    yield  # Application runs here
    
    # ============ SHUTDOWN ============
    try:
        if clientMqqt and clientMqqt.client:
            clientMqqt.client.loop_stop()
            clientMqqt.client.disconnect()
            logger.info("MQTT client disconnected during shutdown")
    except Exception as e:
        logger.error(f"Error during MQTT shutdown: {e}")


app = FastAPI(
    title="Parking Management System API (Oracle)",
    description="Complete parking management with Oracle PL/SQL functions, procedures, triggers",
    version="1.0.0",
    lifespan=lifespan,
)

allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

    
DB_CONFIG = {
    "user": os.getenv("ORACLE_USER", "SYSTEM"),
    "password": os.getenv("ORACLE_PWD", "oracle_password123"),
    "dsn": os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")
}

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRY_MINUTES = int(os.getenv("JWT_EXPIRY_MINUTES", "480"))
PASSWORD_ITERATIONS = 120000
PASSWORD_SALT = os.getenv("PASSWORD_SALT", "parking-oracle-admin-salt")
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        PASSWORD_SALT.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${PASSWORD_SALT}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_str, salt, digest = stored_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations_str),
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def _base64url_encode(raw_value: bytes) -> str:
    return base64.urlsafe_b64encode(raw_value).rstrip(b"=").decode("ascii")


def _base64url_decode(raw_value: str) -> bytes:
    padding = "=" * (-len(raw_value) % 4)
    return base64.urlsafe_b64decode((raw_value + padding).encode("ascii"))


def create_access_token(payload: dict, expires_minutes: int = JWT_EXPIRY_MINUTES) -> str:
    now = datetime.now(timezone.utc)
    token_payload = {
        **payload,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    header = {"alg": JWT_ALGORITHM, "typ": "JWT"}
    header_segment = _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_segment = _base64url_encode(json.dumps(token_payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_segment}.{payload_segment}"
    signature = hmac.new(
        JWT_SECRET_KEY.encode("utf-8"),
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_base64url_encode(signature)}"


def decode_access_token(token: str) -> dict:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        signing_input = f"{header_segment}.{payload_segment}"
        expected_signature = hmac.new(
            JWT_SECRET_KEY.encode("utf-8"),
            signing_input.encode("ascii"),
            hashlib.sha256,
        ).digest()
        actual_signature = _base64url_decode(signature_segment)
        if not hmac.compare_digest(expected_signature, actual_signature):
            raise HTTPException(status_code=401, detail="Invalid token signature")

        payload = json.loads(_base64url_decode(payload_segment).decode("utf-8"))
        if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
            raise HTTPException(status_code=401, detail="Token has expired")
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication token") from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid authentication token") from exc


@contextmanager
def get_db_connection():
    """Context manager for Oracle database connections"""
    conn = None
    try:
        conn = cx_Oracle.connect(**DB_CONFIG)
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if conn:
            conn.close()

def get_db():
    """Dependency for database connection"""
    with get_db_connection() as conn:
        yield conn


def fetch_available_spots(conn):
    """Return currently available and non-reserved spots."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT spot_id, spot_number, spot_type, status, hourly_rate
        FROM spots
        WHERE status = 'AVAILABLE'
          AND NOT EXISTS (
                SELECT 1
                FROM reservations r
                WHERE r.spot_id = spots.spot_id
                  AND r.status = 'CONFIRMED'
                  AND SYSTIMESTAMP BETWEEN r.start_time AND r.end_time
          )
        ORDER BY spot_id
        """
    )
    columns = [col[0].lower() for col in cursor.description]
    spots = [dict(zip(columns, row)) for row in cursor.fetchall()]
    cursor.close()
    return spots


def fetch_available_spots_for_publish(conn):
    """Return a tuple of (is_available, list_of_ids)."""
    cursor = conn.cursor()
    
    query = """
    SELECT spot_id
    FROM spots
    WHERE status = 'AVAILABLE'
    AND NOT EXISTS (
            SELECT 1
            FROM reservations r
            WHERE r.spot_id = spots.spot_id
            AND r.status = 'CONFIRMED'
            AND SYSTIMESTAMP BETWEEN r.start_time AND r.end_time
    )
        ORDER BY spot_id
    FETCH FIRST 8 ROWS ONLY
    """
    
    cursor.execute(query)
    
    spot_ids = [row[0] for row in cursor.fetchall()]
    cursor.close()
    return spot_ids


async def _async_publish_wrapper(topic: str, payload: object, trigger: str = "MQTT_PUBLISH"):
    """Wrapper to handle async MQTT publishing with error handling and logging."""
    try:
        logger.info(f"[{trigger}] Publishing to {topic}: {payload}")
        
        success = await clientMqqt.async_publish_single(
            topic=topic,
            payload=payload,
        )
        if success:
            logger.info(f"[{trigger}] ✓ Message sent to {topic}")
        else:
            logger.warning(f"[{trigger}] ✗ Failed to send message to {topic}")
        return success
    except Exception as e:
        logger.error(f"[{trigger}] Error publishing to {topic}: {e}", exc_info=True)
        return False

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    email: EmailStr
    phone: Optional[str] = None
    user_type: str = Field(default="REGULAR", pattern="^(REGULAR|PREMIUM|VIP)$")

class UserResponse(BaseModel):
    user_id: int
    username: str
    email: str
    phone: Optional[str]
    user_type: str
    role: str
    is_active: str
    money_balance: float
    created_at: datetime

class AdminLogin(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=6, max_length=200)

class UserRegister(BaseModel):
    """Model for user registration"""
    username: str = Field(..., min_length=3, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=200)
    phone: Optional[str] = None
    user_type: str = Field(default="REGULAR", pattern="^(REGULAR|PREMIUM|VIP)$")

class UserLogin(BaseModel):
    """Model for user login"""
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=6, max_length=200)

class AdminUserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=200)
    phone: Optional[str] = None
    user_type: str = Field(default="REGULAR", pattern="^(REGULAR|PREMIUM|VIP)$")
    role: str = Field(default="ADMIN", pattern="^(USER|ADMIN)$")

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: Optional[int] = None
    money_balance: Optional[float] = None
    role: str

class SpotCreate(BaseModel):
    spot_number: str = Field(..., min_length=1, max_length=20)
    spot_type: str = Field(default="STANDARD", pattern="^(STANDARD|DISABLED|EV|LARGE)$")
    status: str = Field(default="AVAILABLE", pattern="^(AVAILABLE|OCCUPIED|RESERVED|MAINTENANCE)$")
    hourly_rate: float = Field(..., ge=0)

class SpotResponse(BaseModel):
    spot_id: int
    spot_number: str
    spot_type: str
    status: str
    hourly_rate: float

class ReservationCreate(BaseModel):
    user_id: int
    spot_id: int
    start_time: datetime
    end_time: datetime

class ReservationResponse(BaseModel):
    reservation_id: int
    user_id: int
    spot_id: int
    start_time: datetime
    end_time: datetime
    status: str
    created_at: datetime

class SessionStart(BaseModel):
    user_id: int
    spot_id: int
    vehicle_plate: str = Field(..., min_length=3, max_length=20)

class SessionResponse(BaseModel):
    session_id: int
    user_id: int
    spot_id: int
    vehicle_plate: str
    entry_time: datetime
    exit_time: Optional[datetime]
    duration_hours: Optional[float]
    total_cost: Optional[float]
    session_status: str


class SpotUsageItem(BaseModel):
    spot_id: int
    spot_number: str
    session_count: int
    unique_users: int


class MonthlySpotUsageResponse(BaseModel):
    month: str
    start_date: datetime
    end_date: datetime
    most_used_spots: List[SpotUsageItem]
    least_used_spots: List[SpotUsageItem]


class DailySessionActivityItem(BaseModel):
    day: datetime
    session_count: int
    unique_users: int


class MonthlySessionActivityResponse(BaseModel):
    month: str
    start_date: datetime
    end_date: datetime
    daily_activity: List[DailySessionActivityItem]


def _parse_month_range(month: Optional[str]) -> tuple[datetime, datetime, str]:
    if month is None or month.strip() == "":
        now = datetime.now()
        start_date = datetime(now.year, now.month, 1)
        month_label = start_date.strftime("%Y-%m")
    else:
        try:
            start_date = datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="month must use YYYY-MM format") from exc
        month_label = month

    if start_date.month == 12:
        end_date = datetime(start_date.year + 1, 1, 1)
    else:
        end_date = datetime(start_date.year, start_date.month + 1, 1)

    return start_date, end_date, month_label


@app.get("/", tags=["Health"])
async def root():
    """API health check endpoint"""
    return {
        "status": "online",
        "service": "Parking Management System",
        "version": "1.0.0",
        "database": "Oracle Database with PL/SQL"
    }


@app.get("/health", tags=["Health"])
async def health_check():
    """Detailed health check with database connectivity"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM v$version WHERE banner LIKE 'Oracle%'")
            db_version = cursor.fetchone()[0]
            cursor.close()
            return {
                "status": "healthy",
                "database": "connected",
                "db_version": db_version
            }
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Database connection failed: {str(e)}")


@app.post("/register", response_model=TokenResponse, tags=["Authentication"])
async def user_register(user: UserRegister, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()  
        password_hash = hash_password(user.password)
        user_id_var = cursor.var(cx_Oracle.NUMBER)
        username_var = cursor.var(str)
        sql = """
            INSERT INTO users (username, email, phone, user_type, password_hash, role, is_active)
            VALUES (:username, :email, :phone, :user_type, :password_hash, 'USER', 'Y')
            RETURNING user_id, username INTO :user_id, :ret_username
        """
        
        cursor.execute(sql, {
            'username': user.username,
            'email': user.email,
            'phone': user.phone or '',
            'user_type': user.user_type,
            'password_hash': password_hash,
            'user_id': user_id_var,
            'ret_username': username_var
        })
        
        conn.commit()
        
        user_id = int(user_id_var.getvalue()[0])
        username = username_var.getvalue()[0]
        
        token = create_access_token({
            "user_id": user_id,
            "username": username,
            "role": "USER",
        })
        pyload = {
            "status": "users",
            "user_id": user_id,
            "username": username,
            "email": user.email,
            "phone": user.phone or '',
            "user_type": user.user_type,
            "role": 'USER',
            "is_active": 'Y',
            "money_balance": 0.0,
            "created_at": datetime.now().isoformat()
        }
        asyncio.create_task(
            
            _async_publish_wrapper2(
                topic="parking/status/update",
                payload=pyload,
                trigger="USER_REGISTER",
            )
        )
        cursor.close()
        
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": JWT_EXPIRY_MINUTES * 60,
            "role": "USER",
        }
    except cx_Oracle.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=400, detail="Username or email already exists")
        raise HTTPException(status_code=400, detail="Registration failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Registration error: {str(e)}")


@app.post("/login", response_model=TokenResponse, tags=["Authentication"])
async def user_login(credentials: UserLogin, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, username, password_hash, role, is_active, money_balance
            FROM users
            WHERE (username = :identifier OR email = :identifier)
              AND role = 'USER'
            """,
            {"identifier": credentials.username},
        )
        row = cursor.fetchone()
        cursor.close()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid username or password")
            
        if row[4] != 'Y':
            raise HTTPException(status_code=401, detail="User account is inactive")

        if not row[2] or not verify_password(credentials.password, row[2]):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        token = create_access_token({
            "user_id": int(row[0]),
            "username": row[1],
            "role": row[3],
        })
        
        return {
            "access_token": token,
            "token_type": "bearer",
            "user_id": int(row[0]),
            "expires_in": JWT_EXPIRY_MINUTES * 60,
            "role": row[3],
            "money_balance": float(row[5]) if row[5] is not None else 0.0
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/login", response_model=TokenResponse, tags=["Authentication"])
async def admin_login(credentials: AdminLogin, conn=Depends(get_db)):
    try:
        logger.info(f"Attempting admin login for: {credentials.username}")
        cursor = conn.cursor()
        cursor.execute(
            """
                SELECT user_id, username, password_hash, role, is_active
                FROM users
                WHERE (username = :identifier OR email = :identifier)
                  AND role = 'ADMIN'
            """,
            {"identifier": credentials.username},
        )
        row = cursor.fetchone()
        cursor.close()

        if not row:
            logger.warning(f"Login failed: user '{credentials.username}' not found or not an ADMIN")
            raise HTTPException(status_code=401, detail="Invalid admin credentials")
            
        if row[4] != 'Y':
            logger.warning(f"Login failed: ADMIN user '{credentials.username}' is inactive")
            raise HTTPException(status_code=401, detail="Invalid admin credentials")

        if not row[2] or not verify_password(credentials.password, row[2]):
            logger.warning(f"Login failed: invalid password for ADMIN user '{credentials.username}'")
            raise HTTPException(status_code=401, detail="Invalid admin credentials")

        logger.info(f"Login successful for ADMIN user '{credentials.username}'")
        token = create_access_token({
            "user_id": int(row[0]),
            "username": row[1],
            "role": row[3],
        })
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": JWT_EXPIRY_MINUTES * 60,
            "user_id": int(row[0]),
            "role": row[3],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/users", response_model=UserResponse, tags=["Users"])
@app.post("/users/", response_model=UserResponse, tags=["Users"], include_in_schema=False)
async def create_user(user: UserCreate, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        sql = """
            INSERT INTO users (username, email, phone, user_type, role)
            VALUES (:username, :email, :phone, :user_type, 'USER')
            RETURNING user_id, username, email, phone, user_type, role,
                      is_active, created_at INTO :user_id, :ret_username, :ret_email,
                      :ret_phone, :ret_user_type, :ret_role, :ret_is_active, :ret_created_at
        """
        
        user_id_var = cursor.var(cx_Oracle.NUMBER)
        username_var = cursor.var(str)
        email_var = cursor.var(str)
        phone_var = cursor.var(str)
        user_type_var = cursor.var(str)
        role_var = cursor.var(str)
        is_active_var = cursor.var(str)
        created_at_var = cursor.var(cx_Oracle.TIMESTAMP)
        
        cursor.execute(sql, {
            'username': user.username,
            'email': user.email,
            'phone': user.phone,
            'user_type': user.user_type,
            'user_id': user_id_var,
            'ret_username': username_var,
            'ret_email': email_var,
            'ret_phone': phone_var,
            'ret_user_type': user_type_var,
            'ret_role': role_var,
            'ret_is_active': is_active_var,
            'ret_created_at': created_at_var
        })
        
        conn.commit()
        cursor.close()
        
        return {
            "user_id": int(user_id_var.getvalue()[0]),
            "username": username_var.getvalue()[0],
            "email": email_var.getvalue()[0],
            "phone": phone_var.getvalue()[0],
            "user_type": user_type_var.getvalue()[0],
            "role": role_var.getvalue()[0],
            "is_active": is_active_var.getvalue()[0],
            "created_at": created_at_var.getvalue()[0]
        }
    except cx_Oracle.IntegrityError:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users", tags=["Users"])
@app.get("/users/", tags=["Users"], include_in_schema=False)
async def get_all_users(skip: int = 0, limit: int = 100, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, username, email, phone, user_type, role, is_active, created_at
            FROM users 
            WHERE is_active = 'Y' AND role != 'ADMIN'
            ORDER BY user_id
            OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
        """, {"skip": skip, "limit": limit})
        
        columns = [col[0].lower() for col in cursor.description]
        users = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return users
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}", response_model=UserResponse, tags=["Users"])
async def get_user(user_id: int, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT user_id, username, email, phone, user_type, role, is_active, money_balance, created_at
            FROM users WHERE user_id = :user_id
        """, {"user_id": user_id})
        
        row = cursor.fetchone()
        cursor.close()
        
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        
        columns = ['user_id', 'username', 'email', 'phone', 'user_type', 'role', 'is_active', 'money_balance', 'created_at']
        return dict(zip(columns, row))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}/stats", tags=["Users"])
async def get_user_statistics(user_id: int, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        
        result_var = cursor.var(str)
        cursor.execute("""
            BEGIN
                :result := get_user_stats_json(:user_id);
            END;
        """, {"user_id": user_id, "result": result_var})
        
        result_json = result_var.getvalue()
        cursor.close()
        
        if result_json:
            stats = json.loads(result_json)
            return stats
        else:
            return {
                "total_sessions": 0,
                "total_spent": 0.0,
                "avg_duration": 0.0,
                "last_parking_date": None
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{user_id}/sessions", tags=["Users"])
async def get_user_sessions(
    user_id: int,
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    conn=Depends(get_db),
):
    try:
        cursor = conn.cursor()
        
        if status:
            cursor.execute("""
                SELECT session_id, user_id, spot_id, vehicle_plate, entry_time, exit_time,
                       duration_hours, total_cost, session_status, created_at
                FROM parking_sessions 
                WHERE user_id = :user_id AND session_status = :status
                ORDER BY session_id DESC
                OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
            """, {"user_id": user_id, "status": status, "skip": skip, "limit": limit})
        else:
            cursor.execute("""
                SELECT session_id, user_id, spot_id, vehicle_plate, entry_time, exit_time,
                       duration_hours, total_cost, session_status, created_at
                FROM parking_sessions 
                WHERE user_id = :user_id
                ORDER BY session_id DESC
                OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
            """, {"user_id": user_id, "skip": skip, "limit": limit})
        
        columns = [col[0].lower() for col in cursor.description]
        sessions = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return sessions
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/spots", tags=["Spots"])
async def get_all_spots(conn=Depends(get_db)):
    try:
        spots = fetch_available_spots(conn)
        return spots
    except Exception as e:
        logger.error(f"[GET_ALL_SPOTS] Error retrieving spots: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/spots/{spot_id}", response_model=SpotResponse, tags=["Spots"])
async def get_spot(spot_id: int, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT spot_id, spot_number, spot_type, status, hourly_rate
            FROM spots
            WHERE spot_id = :spot_id
            """,
            {"spot_id": spot_id},
        )
        row = cursor.fetchone()
        cursor.close()

        if not row:
            raise HTTPException(status_code=404, detail="Spot not found")

        columns = ["spot_id", "spot_number", "spot_type", "status", "hourly_rate"]
        return dict(zip(columns, row))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/spots/{spot_id}/availability", tags=["Spots"])
async def check_availability(spot_id: int, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()

        result_var = cursor.var(cx_Oracle.NUMBER)
        cursor.execute(
            """
            BEGIN
                :result := check_spot_availability(:spot_id);
            END;
            """,
            {"spot_id": spot_id, "result": result_var},
        )

        available = int(result_var.getvalue())
        cursor.close()

        return {
            "spot_id": spot_id,
            "is_available": available == 1,
            "status": "available" if available == 1 else "not_available",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/reservations", tags=["Reservations"])
async def create_reservation(reservation: ReservationCreate, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()

        reservation_id = cursor.var(cx_Oracle.NUMBER)
        message = cursor.var(str)

        cursor.callproc(
            "create_reservation",
            [
                reservation.user_id,
                reservation.spot_id,
                reservation.start_time,
                reservation.end_time,
                reservation_id,
                message,
            ],
        )

        conn.commit()
        cursor.close()

        reservation_id_value = reservation_id.getvalue()
        if reservation_id_value is None:
            raise HTTPException(status_code=400, detail=message.getvalue())

        return {
            "success": True,
            "reservation_id": int(reservation_id_value),
            "message": message.getvalue(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reservations", tags=["Reservations"])
async def get_reservations(skip: int = 0, limit: int = 100, conn=Depends(get_db)):
    """List reservations."""
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT reservation_id, user_id, spot_id, start_time, end_time, status, created_at
            FROM reservations
            ORDER BY reservation_id DESC
            OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
            """,
            {"skip": skip, "limit": limit},
        )

        columns = [col[0].lower() for col in cursor.description]
        reservations = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return reservations
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sessions/start", tags=["Sessions"])
async def start_session(session: SessionStart, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        
        session_id = cursor.var(cx_Oracle.NUMBER)
        message = cursor.var(str)
        
        cursor.callproc('start_parking_session', [
            session.user_id,
            session.spot_id,
            session.vehicle_plate,
            session_id,
            message
        ])
        
        conn.commit()
        cursor.close()
        
        session_id_value = session_id.getvalue()
        message_value = message.getvalue()
        
        if session_id_value is None:
            logger.warning(f"[SESSION_START] Session start failed for spot {session.spot_id}: {message_value}")
            normalized_message = (message_value or "").upper()
            if "SPOT_NOT_AVAILABLE" in normalized_message:
                raise HTTPException(status_code=409, detail="Spot is not available")
            if "USER_NOT_FOUND_OR_INACTIVE" in normalized_message:
                raise HTTPException(status_code=404, detail="User not found or inactive")
            raise HTTPException(status_code=400, detail=message_value or "Failed to start session")

        session_id_int = int(session_id_value)
        logger.info(f"[SESSION_START] Session {session_id_int} started for spot {session.spot_id}")

        try:
            asyncio.create_task(
                _async_publish_wrapper(
                    topic="parking/spots/cmd",
                    payload=fetch_available_spots_for_publish(conn),
                    trigger="SESSION_START_SYNC",
                )
            )
            asyncio.create_task(background_status_update(conn))

        except Exception as sync_exc:
            logger.error(
                f"[SESSION_START_SYNC] Failed to build/publish spot sync payload: {sync_exc}",
                exc_info=True,
            )
        
        return {
            "success": True,
            "session_id": session_id_int,
            "message": message_value,
            "user_id": session.user_id,
            "spot_id": session.spot_id,
            "vehicle_plate": session.vehicle_plate,
            "mqtt_async_publishing": True,
        }
    except cx_Oracle.DatabaseError as e:
        error_obj, = e.args
        error_msg = str(error_obj.message)
        if "SPOT_NOT_AVAILABLE" in error_msg:
            logger.error(f"[SESSION_START] Spot {session.spot_id} not available")
            raise HTTPException(status_code=400, detail="Spot is not available")
        else:
            logger.error(f"[SESSION_START] Database error: {error_msg}")
            raise HTTPException(status_code=500, detail=error_msg)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SESSION_START] Error starting session: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/sessions/{session_id}/end", tags=["Sessions"])
async def end_session(session_id: int, conn=Depends(get_db)):
    try:
        cursor = conn.cursor()
        
        total_cost = cursor.var(cx_Oracle.NUMBER)
        message = cursor.var(str)
        
        cursor.callproc('end_parking_session', [
            session_id,
            total_cost,
            message
        ])

        cursor.execute(
            """
            SELECT spot_id
            FROM parking_sessions
            WHERE session_id = :session_id
            """,
            {"session_id": session_id},
        )
        cursor.fetchone()  # kept for parity; result unused

        conn.commit()
        cursor.close()
        
        cost_value = total_cost.getvalue()
        message_value = message.getvalue()
        
        if cost_value is None:
            logger.warning(f"[SESSION_END] Session end failed for {session_id}: {message_value}")
            raise HTTPException(status_code=404, detail=message_value)

        asyncio.create_task(
            _async_publish_wrapper(
                topic="parking/spots/cmd",
                payload=fetch_available_spots_for_publish(conn),
                trigger="SESSION_END",
            )
        )
    
        asyncio.create_task(background_status_update(conn))


        logger.info(f"[SESSION_END] Session {session_id} ended with cost ${cost_value}")
        
        return {
            "success": True,
            "session_id": session_id,
            "total_cost": float(cost_value),
            "message": message_value,
            "mqtt_async_publishing": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[SESSION_END] Error ending session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
async def background_status_update(conn =Depends(get_db)):
    pyload = {
        "status": "sessions",
        "statistics": await get_overview_statistics(conn),
        "get_monthly_spot_usage": await get_monthly_spot_usage(month=None, limit=100, conn=conn),
        "get_monthly_user_sessions": await get_monthly_user_sessions(month=None, conn=conn),
        "spots": await get_all_spots(conn),
    }
    
    await _async_publish_wrapper2(
        topic="parking/status/update",
        payload=pyload,
        trigger="SESSION_END_STATUS",
    )


@app.get("/sessions", tags=["Sessions"])
@app.get("/sessions/", tags=["Sessions"], include_in_schema=False)
async def get_all_sessions(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    conn=Depends(get_db),
):
    try:
        cursor = conn.cursor()
        
        if status:
            cursor.execute("""
                SELECT session_id, user_id, spot_id, vehicle_plate, entry_time, exit_time,
                       duration_hours, total_cost, session_status, created_at
                FROM parking_sessions 
                WHERE session_status = :status
                ORDER BY session_id DESC
                OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
            """, {"status": status, "skip": skip, "limit": limit})
        else:
            cursor.execute("""
                SELECT session_id, user_id, spot_id, vehicle_plate, entry_time, exit_time,
                       duration_hours, total_cost, session_status, created_at
                FROM parking_sessions 
                ORDER BY session_id DESC
                OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
            """, {"skip": skip, "limit": limit})
        
        columns = [col[0].lower() for col in cursor.description]
        sessions = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return sessions
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}", response_model=SessionResponse, tags=["Sessions"])
async def get_session(session_id: int, conn=Depends(get_db)):
    """Get session by ID"""
    try:
        cursor = conn.cursor()
        cursor.execute("""
             SELECT session_id, user_id, spot_id, vehicle_plate, entry_time, exit_time,
                 duration_hours, total_cost, session_status
            FROM parking_sessions WHERE session_id = :session_id
        """, {"session_id": session_id})
        
        row = cursor.fetchone()
        cursor.close()
        
        if not row:
            raise HTTPException(status_code=404, detail="Session not found")
        
        columns = [
            'session_id', 'user_id', 'spot_id', 'vehicle_plate', 'entry_time', 'exit_time',
            'duration_hours', 'total_cost', 'session_status'
        ]
        return dict(zip(columns, row))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/audit-logs", tags=["Audit"])
@app.get("/audit-logs/", tags=["Audit"], include_in_schema=False)
async def get_audit_logs(skip: int = 0, limit: int = 100, conn=Depends(get_db)):
    """Get audit logs created by triggers"""
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT log_id, action, table_name, record_id, user_id, timestamp_log, details
            FROM parking_audit_log
            ORDER BY timestamp_log DESC
            OFFSET :skip ROWS FETCH NEXT :limit ROWS ONLY
        """, {"skip": skip, "limit": limit})
        
        columns = [col[0].lower() for col in cursor.description]
        logs = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.close()
        return logs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

from datetime import datetime, timedelta
from typing import Optional, List
from pydantic import BaseModel
import logging

# Set up logging 


class SpotUsage(BaseModel):
    spot_id: int
    spot_number: str
    session_count: int
    unique_users: int

class MonthlySpotUsageResponse(BaseModel):
    month: str
    start_date: datetime
    end_date: datetime
    most_used_spots: List[SpotUsage]
    least_used_spots: List[SpotUsage]

class DailyActivity(BaseModel):
    day: datetime
    session_count: int
    unique_users: int

class MonthlySessionActivityResponse(BaseModel):
    month: str
    start_date: datetime
    end_date: datetime
    daily_activity: List[DailyActivity]


def get_month_range(month_str: Optional[str] = None):
    """Parses YYYY-MM or defaults to previous month. Returns (start, end, label)."""
    try:
        if not month_str:
            # Default to last month
            now = datetime.now()
            first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            last_month_date = first_of_this_month - timedelta(days=1)
            start_date = last_month_date.replace(day=1)
            end_date = first_of_this_month
        else:
            start_date = datetime.strptime(month_str, "%Y-%m")
            # End date is first of the next month
            if start_date.month == 12:
                end_date = start_date.replace(year=start_date.year + 1, month=1)
            else:
                end_date = start_date.replace(month=start_date.month + 1)
        
        return start_date, end_date, start_date.strftime("%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format. Use YYYY-MM")


@app.get("/statistics/overview", tags=["Statistics"])
async def get_overview_statistics(conn=Depends(get_db)):
    """Get overall system statistics in a single database trip."""
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT 
                    (SELECT COUNT(*) FROM users WHERE is_active = 'Y') as total_users,
                    (SELECT COUNT(*) FROM parking_sessions WHERE session_status = 'ACTIVE') as active_sessions,
                    (SELECT COUNT(*) FROM parking_sessions WHERE session_status = 'COMPLETED') as completed_sessions,
                    (SELECT NVL(SUM(total_cost), 0) FROM parking_sessions WHERE session_status = 'COMPLETED') as total_revenue,
                    (SELECT COUNT(*) FROM spots WHERE status = 'AVAILABLE') as total_available_spots,
                    (SELECT COUNT(*) FROM spots) as total_spots,
                    (SELECT NVL(AVG(duration_hours), 0) FROM parking_sessions WHERE session_status = 'COMPLETED') as avg_dur
                FROM DUAL
            """)
            row = cursor.fetchone()

        return {
            "total_users": int(row[0]),
            "active_sessions": int(row[1]),
            "completed_sessions": int(row[2]),
            "total_revenue": round(float(row[3]), 2),
            "total_available_spots": int(row[4]),
            "total_spots": int(row[5]),
            "avg_duration": round(float(row[6]), 3  )
        }
    except Exception as e:
        logger.error(f"Stats Error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error while fetching statistics")

@app.get("/admin/spots/usage", response_model=MonthlySpotUsageResponse, tags=["Admin"])
async def get_monthly_spot_usage(
    month: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}$", description="YYYY-MM"),
    limit: int = Query(5, ge=1, le=50),
    conn=Depends(get_db)
):
    """Returns top and bottom performing spots for a specific month."""
    start_date, end_date, month_label = get_month_range(month)
    
    query = """
        SELECT spot_id, spot_number, session_count, unique_users
        FROM (
            SELECT s.spot_id, s.spot_number, 
                   COUNT(ps.session_id) AS session_count,
                   COUNT(DISTINCT ps.user_id) AS unique_users
            FROM spots s
            LEFT JOIN parking_sessions ps ON ps.spot_id = s.spot_id
                AND ps.entry_time >= :start_date AND ps.entry_time < :end_date
            GROUP BY s.spot_id, s.spot_number
            ORDER BY session_count {direction}, s.spot_id ASC
        ) FETCH FIRST :limit ROWS ONLY
    """

    try:
        with conn.cursor() as cursor:
            # Fetch Most Used
            cursor.execute(query.format(direction="DESC"), 
                           {"start_date": start_date, "end_date": end_date, "limit": limit})
            most_rows = cursor.fetchall()

            # Fetch Least Used
            cursor.execute(query.format(direction="ASC"), 
                           {"start_date": start_date, "end_date": end_date, "limit": limit})
            least_rows = cursor.fetchall()

        cols = ["spot_id", "spot_number", "session_count", "unique_users"]
        return {
            "month": month_label,
            "start_date": start_date,
            "end_date": end_date,
            "most_used_spots": [dict(zip(cols, r)) for r in most_rows],
            "least_used_spots": [dict(zip(cols, r)) for r in least_rows]
        }
    except Exception as e:
        logger.error(f"Spot Usage Error: {e}")
        raise HTTPException(status_code=500, detail="Database error occurred.")

@app.get("/admin/users/monthly-sessions", response_model=MonthlySessionActivityResponse, tags=["Admin"])
async def get_monthly_user_sessions(
    month: Optional[str] = Query(None, regex=r"^\d{4}-\d{2}$", description="YYYY-MM"),
    conn=Depends(get_db)
):
    """Daily session breakdown for the month."""
    start_date, end_date, month_label = get_month_range(month)

    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT TRUNC(entry_time) AS day, COUNT(*) AS sessions, COUNT(DISTINCT user_id) AS users
                FROM parking_sessions
                WHERE entry_time >= :start_date AND entry_time < :end_date
                GROUP BY TRUNC(entry_time)
                ORDER BY day ASC
            """, {"start_date": start_date, "end_date": end_date})
            rows = cursor.fetchall()

        return {
            "month": month_label,
            "start_date": start_date,
            "end_date": end_date,
            "daily_activity": [
                {"day": r[0], "session_count": r[1], "unique_users": r[2]} for r in rows
            ]
        }
    except Exception as e:
        logger.error(f"Monthly Sessions Error: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")
# ── SSE state ────────────────────────────────────────────────────────────────
from fastapi.responses import StreamingResponse
import asyncio, json

ADMIN_ID = 1  

_sse_queues: dict[int, list[asyncio.Queue]] = {}

def _register_queue(user_id: int) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _sse_queues.setdefault(user_id, []).append(q)
    return q

def _deregister_queue(user_id: int, q: asyncio.Queue) -> None:
    if user_id in _sse_queues and q in _sse_queues[user_id]:
        _sse_queues[user_id].remove(q)

        if not _sse_queues[user_id]:
            del _sse_queues[user_id]

@app.get("/stream", tags=["SSE"])
async def sse_stream(user_id: int):
    """Server-Sent Events stream per user."""
    q = _register_queue(user_id)

    logger.info(f"[SSE] User {user_id} connected ({len(_sse_queues.get(user_id, []))} connections)")

    async def generate():
        yield ": connected\n\n"
        try:
            while True:
                try:
                    event_data = await asyncio.wait_for(q.get(), timeout=20)

                    payload_str = json.dumps(event_data.get("data", {}), default=str)
                    event_name  = event_data.get("event", "message")

                    yield f"event: {event_name}\ndata: {payload_str}\n\n"

                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

        except asyncio.CancelledError:
            pass

        finally:
            _deregister_queue(user_id, q)
            logger.info(f"[SSE] User {user_id} disconnected")

    return StreamingResponse(generate(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
async def _async_publish_wrapper2(
    topic: str,
    payload: object,
    trigger: str = "SSE_PUBLISH",
) -> bool:
    try:
        queues = _sse_queues.get(ADMIN_ID, [])

        if not queues:
            logger.warning(f"[{trigger}] No SSE clients for admin {ADMIN_ID}")
            return False

        event_data = {"event": topic, "data": payload}
        print(event_data)
        for q in queues:
            await q.put(event_data)

        logger.info(f"[{trigger}] ✓ SSE '{topic}' → admin {ADMIN_ID} ({len(queues)} connections)")
        return True

    except Exception as e:
        logger.error(f"[{trigger}] Error pushing SSE event: {e}", exc_info=True)
        return False
if __name__ == "__main__":
    import uvicorn

    async def publish_startup_spots():
        with get_db_connection() as conn:
            spots = fetch_available_spots_for_publish(conn)
            await _async_publish_wrapper(
                topic="parking/spots/cmd",
                payload=spots,
                trigger="GET_ALL_SPOTS",
            )

    asyncio.run(publish_startup_spots())
    uvicorn.run(app, host="0.0.0.0", port=8000)