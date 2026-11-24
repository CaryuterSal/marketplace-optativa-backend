import os
import time
import jwt
from shared.db_dynamo import get_table

JWT_SECRET = os.environ.get("JWT_SECRET", "change-me-in-production-secret-key")
JWT_ALGORITHM = "HS256"


def issue_jwt(user_id: int, public_id: str, session_id: str, ttl_seconds: int = 86400) -> tuple[str, int]:
    now = int(time.time())
    expires_at = now + ttl_seconds
    payload = {
        "sub": str(user_id),
        "public_id": public_id,
        "sid": session_id,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return token, expires_at


def issue_onboarding_token(country_code: str, phone_number: str) -> str:
    now = int(time.time())
    payload = {
        "type": "onboarding",
        "country_code": country_code,
        "phone_number": phone_number,
        "iat": now,
        "exp": now + 900,  # 15 minutos
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


def require_auth(event: dict) -> dict:
    """
    Valida firma JWT stateless y verifica que SessionID siga activo (no revocado) en DynamoDB.
    Retorna claims {'user_id': int, 'public_id': str, 'session_id': str} o lanza PermissionError.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth_header = headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise PermissionError("Token Bearer faltante o inválido.")

    token = auth_header.split(" ", 1)[1].strip()
    try:
        claims = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise PermissionError("El token de sesión ha expirado.")
    except jwt.InvalidTokenError:
        raise PermissionError("Token de autenticación inválido.")

    session_id = claims.get("sid")
    if not session_id:
        raise PermissionError("Token sin identificador de sesión.")

    sessions_table = get_table("DYNAMO_SESSIONS_TABLE", "UserSessions")
    resp = sessions_table.get_item(Key={"SessionID": session_id})
    item = resp.get("Item")
    if not item or int(item.get("ExpiresAt", 0)) < int(time.time()):
        raise PermissionError("La sesión fue cerrada o expiró.")

    return {
        "user_id": int(claims["sub"]),
        "public_id": claims["public_id"],
        "session_id": session_id,
    }
