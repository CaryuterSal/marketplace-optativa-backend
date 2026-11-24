import random
import time
import uuid
from datetime import datetime, timezone
from shared.db_mysql import get_connection
from shared.db_dynamo import get_table
from shared.jwt_auth import (
    issue_jwt,
    issue_onboarding_token,
    decode_token,
    require_auth,
)
from shared.http_responses import response, error_response, parse_body


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        if method == "POST" and path.endswith("/auth/phone/send-code"):
            return handle_send_code(event)
        if method == "POST" and path.endswith("/auth/phone/verify"):
            return handle_verify_code(event)
        if method == "POST" and path.endswith("/auth/register"):
            return handle_register(event)
        if method == "POST" and path.endswith("/auth/logout"):
            return handle_logout(event)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def handle_send_code(event):
    body = parse_body(event)
    country_code = (body.get("country_code") or "").upper().strip()
    phone_number = (body.get("phone_number") or "").strip()

    if len(country_code) != 2 or not phone_number:
        raise ValueError("country_code (2 letras) y phone_number son obligatorios.")

    code = f"{random.randint(10000, 99999)}"
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM country WHERE code = %s", (country_code,))
        country = cur.fetchone()
        if not country:
            cur.execute("INSERT INTO country (code) VALUES (%s)", (country_code,))
            country_id = cur.lastrowid
        else:
            country_id = country["id"]

        cur.execute(
            """
            INSERT INTO phone_numbers (rest_number, country_id, validated_at)
            VALUES (%s, %s, NULL)
            ON DUPLICATE KEY UPDATE rest_number = VALUES(rest_number)
            """,
            (phone_number, country_id),
        )
        cur.execute(
            """
            INSERT INTO validation_codes (code, phone_numbers_country_code, phone_numbers_rest_number)
            VALUES (%s, %s, %s)
            """,
            (code, country_code, phone_number),
        )
    conn.commit()

    # Nota: En producción integrar AWS SNS aquí para enviar `code` por SMS
    return response(200, {
        "message": "Código de 5 dígitos enviado.",
        "expires_in_seconds": 300,
    })


def handle_verify_code(event):
    body = parse_body(event)
    country_code = (body.get("country_code") or "").upper().strip()
    phone_number = (body.get("phone_number") or "").strip()
    code = (body.get("code") or "").strip()

    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM validation_codes
            WHERE phone_numbers_country_code = %s
              AND phone_numbers_rest_number = %s
              AND code = %s
            ORDER BY id DESC LIMIT 1
            """,
            (country_code, phone_number, code),
        )
        valid_row = cur.fetchone()
        if not valid_row:
            return error_response(400, "INVALID_OTP", "Código incorrecto o expirado.")

        # Marcar teléfono como validado y limpiar códigos usados
        cur.execute(
            """
            UPDATE phone_numbers pn
            JOIN country c ON pn.country_id = c.id
            SET pn.validated_at = NOW()
            WHERE c.code = %s AND pn.rest_number = %s
            """,
            (country_code, phone_number),
        )
        cur.execute(
            "DELETE FROM validation_codes WHERE phone_numbers_country_code = %s AND phone_numbers_rest_number = %s",
            (country_code, phone_number),
        )

        # Buscar si el usuario ya existe
        cur.execute(
            """
            SELECT u.id, u.public_id, u.first_name, u.last_name, u.birthday, u.created_at,
                   s.id AS seller_id, s.rfc
            FROM users u
            LEFT JOIN sellers s ON s.users_id = u.id
            WHERE u.phone_numbers_country_code = %s
              AND u.phone_numbers_rest_number = %s
              AND u.deleted_at IS NULL
            """,
            (country_code, phone_number),
        )
        user = cur.fetchone()
    conn.commit()

    if not user:
        onboarding_token = issue_onboarding_token(country_code, phone_number)
        return response(200, {
            "is_new_user": True,
            "onboarding_token": onboarding_token,
            "access_token": None,
            "expires_in": 900,
        })

    session_data = _create_dynamo_session(event, user["id"], user["public_id"])
    return response(200, {
        "is_new_user": False,
        "onboarding_token": None,
        "access_token": session_data["access_token"],
        "expires_in": 86400,
        "user": _format_user_profile(user, country_code, phone_number),
    })


def handle_register(event):
    body = parse_body(event)
    onboarding_token = body.get("onboarding_token")
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    birthday = body.get("birthday")

    if not onboarding_token or not first_name or not last_name:
        raise ValueError("onboarding_token, first_name y last_name son obligatorios.")

    claims = decode_token(onboarding_token)
    if claims.get("type") != "onboarding":
        raise PermissionError("Token de registro inválido.")

    country_code = claims["country_code"]
    phone_number = claims["phone_number"]
    public_id = f"usr_{uuid.uuid4().hex[:20]}"

    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (public_id, first_name, last_name, birthday, created_at,
                               phone_numbers_country_code, phone_numbers_rest_number)
            VALUES (%s, %s, %s, %s, NOW(), %s, %s)
            """,
            (public_id, first_name, last_name, birthday, country_code, phone_number),
        )
        user_id = cur.lastrowid
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    conn.commit()

    session_data = _create_dynamo_session(event, user_id, public_id)
    return response(201, {
        "is_new_user": False,
        "onboarding_token": None,
        "access_token": session_data["access_token"],
        "expires_in": 86400,
        "user": _format_user_profile(user, country_code, phone_number),
    })


def handle_logout(event):
    auth = require_auth(event)
    sessions_table = get_table("DYNAMO_SESSIONS_TABLE", "UserSessions")
    sessions_table.delete_item(Key={"SessionID": auth["session_id"]})
    return response(204)


def _create_dynamo_session(event: dict, user_id: int, public_id: str) -> dict:
    session_id = f"SESS-{uuid.uuid4()}"
    token, expires_at = issue_jwt(user_id, public_id, session_id, ttl_seconds=86400)

    req_ctx = event.get("requestContext", {})
    http_info = req_ctx.get("http", {})
    ip_addr = http_info.get("sourceIp") or req_ctx.get("identity", {}).get("sourceIp", "0.0.0.0")
    user_agent = http_info.get("userAgent") or (event.get("headers") or {}).get("User-Agent", "Unknown")

    sessions_table = get_table("DYNAMO_SESSIONS_TABLE", "UserSessions")
    sessions_table.put_item(Item={
        "SessionID": session_id,
        "UserID": str(user_id),
        "AuthToken": token,
        "IPAddress": ip_addr,
        "DeviceInfo": user_agent[:120],
        "CreatedAt": datetime.now(timezone.utc).isoformat(),
        "ExpiresAt": expires_at,
    })
    return {"session_id": session_id, "access_token": token}


def _format_user_profile(user: dict, country_code: str, phone_number: str) -> dict:
    is_seller = bool(user.get("seller_id"))
    return {
        "public_id": user["public_id"],
        "first_name": user["first_name"],
        "last_name": user["last_name"],
        "birthday": user.get("birthday"),
        "phone": {
            "country_code": country_code,
            "number": phone_number,
            "validated": True,
        },
        "is_seller": is_seller,
        "seller_info": {
            "seller_id": user["seller_id"],
            "rfc": user["rfc"],
        } if is_seller else None,
        "created_at": user["created_at"],
    }
