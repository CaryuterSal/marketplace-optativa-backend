import json
import uuid
from shared.db_mysql import get_connection
from shared.db_dynamo import get_table
from shared.jwt_auth import require_auth
from shared.http_responses import response, error_response, parse_body, get_path_param


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        auth = require_auth(event)
        if method == "GET" and path.endswith("/cart"):
            return handle_get_cart(auth["user_id"])
        if method == "POST" and path.endswith("/cart/items"):
            return handle_upsert_cart_item(auth["user_id"], event)
        if method == "DELETE" and "/cart/items/" in path:
            item_id = get_path_param(event, "item_id") or path.rstrip("/").split("/")[-1]
            return handle_delete_cart_item(auth["user_id"], item_id)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def _get_or_create_active_cart(cur, user_id: int) -> dict:
    # Un carrito está activo si aún no se convierte en registro de `order`
    cur.execute(
        """
        SELECT c.id, c.public_id, c.updated_at
        FROM carts c
        LEFT JOIN `order` o ON o.carts_id = c.id
        WHERE c.users_id = %s AND o.carts_id IS NULL
        ORDER BY c.id DESC LIMIT 1
        """,
        (user_id,),
    )
    cart = cur.fetchone()
    if cart:
        return cart

    new_uuid = uuid.uuid4().bytes
    cur.execute(
        "INSERT INTO carts (public_id, created_at, users_id) VALUES (%s, NOW(), %s)",
        (new_uuid, user_id),
    )
    return {"id": cur.lastrowid, "public_id": new_uuid, "updated_at": None}


def handle_get_cart(user_id: int):
    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cart = _get_or_create_active_cart(cur, user_id)
        cur.execute(
            "SELECT id, products_id, quantity, selected_attributes FROM cart_items WHERE carts_id = %s",
            (cart["id"],),
        )
        raw_items = cur.fetchall()
    conn_comm.commit()

    if not raw_items:
        return response(200, {
            "cart_public_id": str(uuid.UUID(bytes=cart["public_id"])),
            "items": [],
            "subtotal": 0.0,
            "discount_total": 0.0,
            "estimated_total": 0.0,
            "updated_at": cart["updated_at"],
        })

    prod_ids = list({r["products_id"] for r in raw_items})
    placeholders = ",".join(["%s"] * len(prod_ids))

    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute(
            f"SELECT id, public_id, name, sellers_id, price FROM products WHERE id IN ({placeholders})",
            tuple(prod_ids),
        )
        prods = {r["id"]: r for r in cur.fetchall()}

        cur.execute(
            f"""
            SELECT products_id, discount_type, discount_value
            FROM discounts
            WHERE products_id IN ({placeholders})
              AND is_active = 1 AND start_date <= NOW() AND end_date >= NOW()
            """,
            tuple(prod_ids),
        )
        discounts = {r["products_id"]: r for r in cur.fetchall()}

    dynamo_prods = get_table("DYNAMO_PRODUCTS_TABLE", "Products")
    items_out = []
    subtotal = 0.0
    discount_total = 0.0

    for item in raw_items:
        p = prods.get(item["products_id"])
        if not p:
            continue
        pub_uuid = str(uuid.UUID(bytes=p["public_id"]))
        unit_price = float(p["price"])
        final_unit = unit_price

        disc = discounts.get(p["id"])
        if disc:
            d_val = float(disc["discount_value"])
            if disc["discount_type"] == "PERCENTAGE":
                final_unit = round(unit_price * (1 - d_val / 100.0), 2)
            else:
                final_unit = max(0.0, round(unit_price - d_val, 2))

        qty = int(item["quantity"])
        line_sub = round(final_unit * qty, 2)
        subtotal += round(unit_price * qty, 2)
        discount_total += round((unit_price - final_unit) * qty, 2)

        dyn = dynamo_prods.get_item(Key={"ProductID": pub_uuid}).get("Item", {})
        images = dyn.get("Images", [])

        sel_attrs = item.get("selected_attributes")
        if isinstance(sel_attrs, str):
            sel_attrs = json.loads(sel_attrs)

        items_out.append({
            "item_id": item["id"],
            "product_public_id": pub_uuid,
            "product_name": p["name"],
            "image": images[0] if images else None,
            "seller_id": p["sellers_id"],
            "quantity": qty,
            "unit_price": unit_price,
            "final_unit_price": final_unit,
            "subtotal": line_sub,
            "selected_attributes": sel_attrs,
        })

    return response(200, {
        "cart_public_id": str(uuid.UUID(bytes=cart["public_id"])),
        "items": items_out,
        "subtotal": round(subtotal, 2),
        "discount_total": round(discount_total, 2),
        "estimated_total": round(subtotal - discount_total, 2),
        "updated_at": cart["updated_at"],
    })


def handle_upsert_cart_item(user_id: int, event: dict):
    body = parse_body(event)
    pub_id = body.get("product_public_id")
    quantity = int(body.get("quantity", 1))
    selected_attributes = body.get("selected_attributes")

    if not pub_id or quantity < 1:
        raise ValueError("product_public_id y quantity (>= 1) son obligatorios.")

    uuid_bytes = uuid.UUID(pub_id).bytes
    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, COALESCE(SUM(i.stock_quantity), 0) AS stock
            FROM products p
            LEFT JOIN inventory i ON i.products_id = p.id
            WHERE p.public_id = %s AND p.deleted_at IS NULL
            GROUP BY p.id
            """,
            (uuid_bytes,),
        )
        prod = cur.fetchone()
        if not prod:
            return error_response(404, "PRODUCT_NOT_FOUND", "El producto no existe.")
        if int(prod["stock"]) < quantity:
            return error_response(409, "OUT_OF_STOCK", f"Stock disponible: {prod['stock']}.")

    attrs_json = json.dumps(selected_attributes, sort_keys=True) if selected_attributes else None

    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cart = _get_or_create_active_cart(cur, user_id)
        cur.execute(
            """
            SELECT id FROM cart_items
            WHERE carts_id = %s AND products_id = %s
              AND (selected_attributes <=> %s)
            """,
            (cart["id"], prod["id"], attrs_json),
        )
        existing = cur.fetchone()
        if existing:
            cur.execute(
                "UPDATE cart_items SET quantity = %s WHERE id = %s",
                (quantity, existing["id"]),
            )
        else:
            item_id = f"ci_{uuid.uuid4().hex[:12]}"
            cur.execute(
                """
                INSERT INTO cart_items (id, carts_id, products_id, quantity, selected_attributes)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (item_id, cart["id"], prod["id"], quantity, attrs_json),
            )
        cur.execute("UPDATE carts SET updated_at = NOW() WHERE id = %s", (cart["id"],))
    conn_comm.commit()

    return handle_get_cart(user_id)


def handle_delete_cart_item(user_id: int, item_id: str):
    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cart = _get_or_create_active_cart(cur, user_id)
        cur.execute(
            "DELETE FROM cart_items WHERE id = %s AND carts_id = %s",
            (item_id, cart["id"]),
        )
        cur.execute("UPDATE carts SET updated_at = NOW() WHERE id = %s", (cart["id"],))
    conn_comm.commit()
    return handle_get_cart(user_id)
