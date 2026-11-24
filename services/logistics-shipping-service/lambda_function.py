from shared.db_mysql import get_connection
from shared.jwt_auth import require_auth
from shared.http_responses import response, error_response, parse_body, get_path_param, get_query_param


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        auth = require_auth(event)
        if method == "GET" and path.endswith("/checkout/shipping-options"):
            return handle_shipping_options(auth["user_id"], event)
        if method == "GET" and path.endswith("/sellers/me/shipments"):
            return handle_seller_list_shipments(auth["user_id"])
        if method == "PATCH" and "/sellers/me/shipments/" in path:
            shipment_id = int(get_path_param(event, "shipment_id") or path.rstrip("/").split("/")[-1])
            return handle_seller_update_shipment(auth["user_id"], shipment_id, event)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def handle_shipping_options(user_id: int, event: dict):
    address_id = get_query_param(event, "address_id")
    if not address_id:
        raise ValueError("El parámetro query 'address_id' es obligatorio.")

    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cur.execute("SELECT id FROM address WHERE id = %s AND users_id = %s", (address_id, user_id))
        if not cur.fetchone():
            raise ValueError("address_id no válida.")

        cur.execute(
            """
            SELECT ci.products_id
            FROM carts c
            JOIN cart_items ci ON ci.carts_id = c.id
            LEFT JOIN `order` o ON o.carts_id = c.id
            WHERE c.users_id = %s AND o.carts_id IS NULL
            """,
            (user_id,),
        )
        prod_ids = list({r["products_id"] for r in cur.fetchall()})
        if not prod_ids:
            return response(200, [])

        cur.execute("SELECT id, name, carrier, estimated_days, base_cost FROM shipping_methods")
        methods = cur.fetchall()

    placeholders = ",".join(["%s"] * len(prod_ids))
    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur_cat:
        cur_cat.execute(
            f"SELECT DISTINCT sellers_id FROM products WHERE id IN ({placeholders})",
            tuple(prod_ids),
        )
        seller_ids = [r["sellers_id"] for r in cur_cat.fetchall()]

    quotes = []
    for sid in seller_ids:
        quotes.append({
            "seller_id": sid,
            "methods": [
                {
                    "shipping_method_id": m["id"],
                    "name": m["name"],
                    "carrier": m["carrier"],
                    "estimated_days": m["estimated_days"],
                    "cost": m["base_cost"],
                }
                for m in methods
            ],
        })
    return response(200, quotes)


def _get_seller_id(cur, user_id: int) -> int:
    cur.execute("SELECT id FROM sellers WHERE users_id = %s", (user_id,))
    seller = cur.fetchone()
    if not seller:
        raise PermissionError("El usuario no está registrado como vendedor.")
    return seller["id"]


def handle_seller_list_shipments(user_id: int):
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        seller_id = _get_seller_id(cur, user_id)
        cur.execute(
            """
            SELECT s.id, s.sellers_id AS seller_id, s.tracking_number, sm.carrier,
                   s.status, s.shipping_cost, s.shipped_at, s.estimated_delivered_at, s.delivered_at
            FROM shipments s
            JOIN shipping_methods sm ON s.shipping_methods_id = sm.id
            WHERE s.sellers_id = %s
            ORDER BY s.id DESC
            """,
            (seller_id,),
        )
        rows = cur.fetchall()

    for r in rows:
        if not r["tracking_number"]:
            r["tracking_number"] = None
    return response(200, rows)


def handle_seller_update_shipment(user_id: int, shipment_id: int, event: dict):
    body = parse_body(event)
    tracking_number = body.get("tracking_number")
    status = body.get("status")

    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        seller_id = _get_seller_id(cur, user_id)
        cur.execute(
            "SELECT id FROM shipments WHERE id = %s AND sellers_id = %s",
            (shipment_id, seller_id),
        )
        if not cur.fetchone():
            return error_response(404, "SHIPMENT_NOT_FOUND", "Envío no encontrado para este vendedor.")

        updates, params = [], []
        if tracking_number is not None:
            updates.append("tracking_number = %s")
            params.append(tracking_number)
        if status:
            updates.append("status = %s")
            params.append(status)
            if status == "SHIPPED":
                updates.append("shipped_at = NOW()")
            elif status == "DELIVERED":
                updates.append("delivered_at = NOW()")

        if not updates:
            raise ValueError("Debes enviar tracking_number o status.")

        params.append(shipment_id)
        cur.execute(f"UPDATE shipments SET {', '.join(updates)} WHERE id = %s", tuple(params))

        cur.execute(
            """
            SELECT s.id, s.sellers_id AS seller_id, s.tracking_number, sm.carrier,
                   s.status, s.shipping_cost, s.shipped_at, s.estimated_delivered_at, s.delivered_at
            FROM shipments s
            JOIN shipping_methods sm ON s.shipping_methods_id = sm.id
            WHERE s.id = %s
            """,
            (shipment_id,),
        )
        updated = cur.fetchone()
    conn.commit()
    return response(200, updated)
