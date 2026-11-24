from shared.db_mysql import get_connection
from shared.jwt_auth import require_auth
from shared.http_responses import response, error_response, parse_body


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        auth = require_auth(event)
        if path.endswith("/users/me/payment-methods"):
            if method == "GET":
                return handle_list_payment_methods(auth["user_id"])
            if method == "POST":
                return handle_create_payment_method(auth["user_id"], event)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def handle_list_payment_methods(user_id: int):
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, provider, payment_type, provider_token, card_brand,
                   card_last_four, exp_month, exp_year, is_default,
                   billing_address_id, created_at
            FROM payment_methods
            WHERE users_id = %s
            ORDER BY is_default DESC, id DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

    for r in rows:
        r["is_default"] = bool(r["is_default"])
    return response(200, rows)


def handle_create_payment_method(user_id: int, event: dict):
    body = parse_body(event)
    required = ["provider", "payment_type", "provider_token", "billing_address_id"]
    for req in required:
        if not body.get(req):
            raise ValueError(f"El campo '{req}' es obligatorio.")

    is_default = 1 if body.get("is_default") else 0
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        # Validar que la dirección de facturación pertenezca al usuario
        cur.execute(
            "SELECT id FROM address WHERE id = %s AND users_id = %s",
            (body["billing_address_id"], user_id),
        )
        if not cur.fetchone():
            raise ValueError("billing_address_id no válida para este usuario.")

        if is_default:
            cur.execute("UPDATE payment_methods SET is_default = 0 WHERE users_id = %s", (user_id,))

        cur.execute(
            """
            INSERT INTO payment_methods (
                provider, payment_type, provider_token, card_brand, card_last_four,
                exp_month, exp_year, is_default, created_at, users_id, billing_address_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s)
            """,
            (
                body["provider"],
                body["payment_type"],
                body["provider_token"],
                body.get("card_brand"),
                body.get("card_last_four"),
                body.get("exp_month"),
                body.get("exp_year"),
                is_default,
                user_id,
                body["billing_address_id"],
            ),
        )
        pm_id = cur.lastrowid
        cur.execute("SELECT * FROM payment_methods WHERE id = %s", (pm_id,))
        row = cur.fetchone()
    conn.commit()

    row["is_default"] = bool(row["is_default"])
    return response(201, row)
