from shared.db_mysql import get_connection
from shared.jwt_auth import require_auth
from shared.http_responses import response, error_response, parse_body


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        auth = require_auth(event)
        if path.endswith("/users/me"):
            if method == "GET":
                return handle_get_profile(auth["user_id"])
            if method == "PATCH":
                return handle_update_profile(auth["user_id"], event)
        if path.endswith("/users/me/addresses"):
            if method == "GET":
                return handle_list_addresses(auth["user_id"])
            if method == "POST":
                return handle_create_address(auth["user_id"], event)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def handle_get_profile(user_id: int):
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.public_id, u.first_name, u.last_name, u.birthday, u.created_at,
                   u.phone_numbers_country_code, u.phone_numbers_rest_number,
                   s.id AS seller_id, s.rfc
            FROM users u
            LEFT JOIN sellers s ON s.users_id = u.id
            WHERE u.id = %s AND u.deleted_at IS NULL
            """,
            (user_id,),
        )
        u = cur.fetchone()
        if not u:
            return error_response(404, "USER_NOT_FOUND", "Usuario no encontrado.")

    is_seller = bool(u.get("seller_id"))
    return response(200, {
        "public_id": u["public_id"],
        "first_name": u["first_name"],
        "last_name": u["last_name"],
        "birthday": u["birthday"],
        "phone": {
            "country_code": u["phone_numbers_country_code"],
            "number": u["phone_numbers_rest_number"],
            "validated": True,
        },
        "is_seller": is_seller,
        "seller_info": {"seller_id": u["seller_id"], "rfc": u["rfc"]} if is_seller else None,
        "created_at": u["created_at"],
    })


def handle_update_profile(user_id: int, event: dict):
    body = parse_body(event)
    fields, values = [], []
    for col in ("first_name", "last_name", "birthday"):
        if col in body and body[col] is not None:
            fields.append(f"{col} = %s")
            values.append(body[col])

    if not fields:
        raise ValueError("Debes enviar al menos un campo para actualizar.")

    values.append(user_id)
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE users SET {', '.join(fields)}, updated_at = NOW() WHERE id = %s",
            tuple(values),
        )
    conn.commit()
    return handle_get_profile(user_id)


def handle_list_addresses(user_id: int):
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.id, c.code AS country_code, pc.code AS postal_code,
                   a.state, a.city, a.street, a.ext_num, a.int_num, a.comments,
                   ST_Y(a.geo_point) AS lat, ST_X(a.geo_point) AS lng
            FROM address a
            JOIN postal_codes pc ON a.postal_codes_id = pc.id
            JOIN country c ON pc.country_id = c.id
            WHERE a.users_id = %s
            ORDER BY a.id DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

    items = []
    for r in rows:
        coords = {"lat": r["lat"], "lng": r["lng"]} if r["lat"] is not None else None
        items.append({
            "id": r["id"],
            "country_code": r["country_code"],
            "postal_code": r["postal_code"],
            "state": r["state"],
            "city": r["city"],
            "street": r["street"],
            "ext_num": r["ext_num"],
            "int_num": r["int_num"],
            "comments": r["comments"],
            "coordinates": coords,
        })
    return response(200, items)


def handle_create_address(user_id: int, event: dict):
    body = parse_body(event)
    required = ["country_code", "postal_code", "state", "city", "street", "ext_num"]
    for req in required:
        if not body.get(req):
            raise ValueError(f"El campo '{req}' es obligatorio.")

    country_code = body["country_code"].upper().strip()
    postal_code = body["postal_code"].strip()
    coords = body.get("coordinates")

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
            "SELECT id FROM postal_codes WHERE code = %s AND country_id = %s",
            (postal_code, country_id),
        )
        pc = cur.fetchone()
        if not pc:
            cur.execute(
                "INSERT INTO postal_codes (code, country_id) VALUES (%s, %s)",
                (postal_code, country_id),
            )
            pc_id = cur.lastrowid
        else:
            pc_id = pc["id"]

        if coords and coords.get("lat") is not None and coords.get("lng") is not None:
            wkt = f"POINT({float(coords['lng'])} {float(coords['lat'])})"
            cur.execute(
                """
                INSERT INTO address (state, city, street, ext_num, int_num, comments,
                                     postal_codes_id, users_id, geo_point)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, ST_GeomFromText(%s))
                """,
                (
                    body["state"], body["city"], body["street"], body["ext_num"],
                    body.get("int_num"), body.get("comments"), pc_id, user_id, wkt,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO address (state, city, street, ext_num, int_num, comments,
                                     postal_codes_id, users_id, geo_point)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
                """,
                (
                    body["state"], body["city"], body["street"], body["ext_num"],
                    body.get("int_num"), body.get("comments"), pc_id, user_id,
                ),
            )
        addr_id = cur.lastrowid
    conn.commit()

    return response(201, {
        "id": addr_id,
        "country_code": country_code,
        "postal_code": postal_code,
        "state": body["state"],
        "city": body["city"],
        "street": body["street"],
        "ext_num": body["ext_num"],
        "int_num": body.get("int_num"),
        "comments": body.get("comments"),
        "coordinates": coords,
    })
