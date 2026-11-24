import json
import math
import uuid
from collections import defaultdict
from shared.db_mysql import get_connection
from shared.jwt_auth import require_auth
from shared.http_responses import response, error_response, parse_body, get_path_param, get_query_param


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        auth = require_auth(event)
        if method == "POST" and path.endswith("/checkout"):
            return handle_checkout(auth["user_id"], event)
        if method == "GET" and path.endswith("/orders"):
            return handle_list_orders(auth["user_id"], event)
        if method == "GET" and "/orders/" in path:
            order_id = int(get_path_param(event, "order_id") or path.rstrip("/").split("/")[-1])
            return handle_get_order(auth["user_id"], order_id)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def handle_checkout(user_id: int, event: dict):
    body = parse_body(event)
    payment_method_id = body.get("payment_method_id")
    shipping_address_id = body.get("shipping_address_id")
    shipping_selections = {
        int(s["seller_id"]): int(s["shipping_method_id"])
        for s in body.get("seller_shipping_selections", [])
    }

    if not payment_method_id or not shipping_address_id or not shipping_selections:
        raise ValueError("payment_method_id, shipping_address_id y seller_shipping_selections son obligatorios.")

    conn_comm = get_connection("db_commerce")
    conn_cat = get_connection("db_catalog")

    try:
        with conn_comm.cursor() as cur_comm, conn_cat.cursor() as cur_cat:
            # 1. Obtener carrito activo
            cur_comm.execute(
                """
                SELECT c.id FROM carts c
                LEFT JOIN `order` o ON o.carts_id = c.id
                WHERE c.users_id = %s AND o.carts_id IS NULL
                ORDER BY c.id DESC LIMIT 1
                """,
                (user_id,),
            )
            cart = cur_comm.fetchone()
            if not cart:
                raise ValueError("No hay un carrito activo para procesar.")
            cart_id = cart["id"]

            cur_comm.execute(
                "SELECT id, products_id, quantity, selected_attributes FROM cart_items WHERE carts_id = %s",
                (cart_id,),
            )
            cart_items = cur_comm.fetchall()
            if not cart_items:
                raise ValueError("El carrito está vacío.")

            # 2. Bloquear inventario y calcular precios en db_catalog
            prod_ids = list({i["products_id"] for i in cart_items})
            placeholders = ",".join(["%s"] * len(prod_ids))
            cur_cat.execute(
                f"SELECT id, public_id, name, sellers_id, price FROM products WHERE id IN ({placeholders})",
                tuple(prod_ids),
            )
            prods = {r["id"]: r for r in cur_cat.fetchall()}

            cur_cat.execute(
                f"""
                SELECT products_id, discount_type, discount_value
                FROM discounts
                WHERE products_id IN ({placeholders})
                  AND is_active = 1 AND start_date <= NOW() AND end_date >= NOW()
                """,
                tuple(prod_ids),
            )
            discounts = {r["products_id"]: r for r in cur_cat.fetchall()}

            subtotal = 0.0
            discount_total = 0.0
            seller_groups = defaultdict(list)

            for ci in cart_items:
                pid = ci["products_id"]
                qty = int(ci["quantity"])
                prod = prods[pid]

                # Validar y descontar stock con bloqueo pesimista
                cur_cat.execute(
                    "SELECT id, stock_quantity FROM inventory WHERE products_id = %s FOR UPDATE",
                    (pid,),
                )
                inv_rows = cur_cat.fetchall()
                total_stock = sum(r["stock_quantity"] for r in inv_rows)
                if total_stock < qty:
                    conn_cat.rollback()
                    conn_comm.rollback()
                    return error_response(409, "OUT_OF_STOCK", f"Stock insuficiente para {prod['name']}.")

                remaining = qty
                for inv in inv_rows:
                    if remaining <= 0:
                        break
                    deduct = min(inv["stock_quantity"], remaining)
                    if deduct > 0:
                        cur_cat.execute(
                            "UPDATE inventory SET stock_quantity = stock_quantity - %s WHERE id = %s",
                            (deduct, inv["id"]),
                        )
                        remaining -= deduct

                unit_price = float(prod["price"])
                final_unit = unit_price
                disc = discounts.get(pid)
                if disc:
                    d_val = float(disc["discount_value"])
                    if disc["discount_type"] == "PERCENTAGE":
                        final_unit = round(unit_price * (1 - d_val / 100.0), 2)
                    else:
                        final_unit = max(0.0, round(unit_price - d_val, 2))

                line_final = round(final_unit * qty, 2)
                subtotal += round(unit_price * qty, 2)
                discount_total += round((unit_price - final_unit) * qty, 2)

                seller_groups[int(prod["sellers_id"])].append({
                    "cart_item": ci,
                    "product": prod,
                    "unit_price": unit_price,
                    "final_price": line_final,
                })

            # 3. Calcular costos de envío por cada seller involucrado
            shipping_total = 0.0
            seller_ship_methods = {}
            for seller_id in seller_groups.keys():
                sm_id = shipping_selections.get(seller_id)
                if not sm_id:
                    raise ValueError(f"Falta seleccionar método de envío para el vendedor {seller_id}.")
                cur_comm.execute("SELECT * FROM shipping_methods WHERE id = %s", (sm_id,))
                sm = cur_comm.fetchone()
                if not sm:
                    raise ValueError(f"shipping_method_id {sm_id} no existe.")
                shipping_total += float(sm["base_cost"])
                seller_ship_methods[seller_id] = sm

            total = round(subtotal - discount_total + shipping_total, 2)

            # 4. Crear orden principal
            cur_comm.execute(
                """
                INSERT INTO `order` (
                    carts_id, users_id, payment_methods_id, shipping_address_id,
                    status, created_at, total, subtotal, discount_total, shipping_total
                )
                VALUES (%s, %s, %s, %s, 'PAID', NOW(), %s, %s, %s, %s)
                """,
                (cart_id, user_id, payment_method_id, shipping_address_id, total, subtotal, discount_total, shipping_total),
            )

            # 5. Crear order_items, shipments (1 por seller) y shipment_items
            for seller_id, entries in seller_groups.items():
                sm = seller_ship_methods[seller_id]
                cur_comm.execute(
                    """
                    INSERT INTO shipments (
                        tracking_number, shipping_cost, status, order_carts_id,
                        shipping_methods_id, sellers_id, address_id
                    )
                    VALUES ('', %s, 'PREPARING', %s, %s, %s, %s)
                    """,
                    (sm["base_cost"], cart_id, sm["id"], seller_id, shipping_address_id),
                )
                shipment_id = cur_comm.lastrowid

                for entry in entries:
                    ci = entry["cart_item"]
                    cur_comm.execute(
                        """
                        INSERT INTO order_items (
                            cart_items_id, products_id, quantity, unit_price,
                            final_price, order_carts_id, selected_attributes
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            ci["id"], ci["products_id"], ci["quantity"],
                            entry["unit_price"], entry["final_price"], cart_id,
                            ci.get("selected_attributes"),
                        ),
                    )
                    oi_id = cur_comm.lastrowid
                    cur_comm.execute(
                        "INSERT INTO shipment_items (quantity, shipments_id, order_items_id) VALUES (%s, %s, %s)",
                        (ci["quantity"], shipment_id, oi_id),
                    )

        conn_cat.commit()
        conn_comm.commit()
    except Exception:
        conn_cat.rollback()
        conn_comm.rollback()
        raise

    return handle_get_order(user_id, cart_id, status_code=201)


def handle_list_orders(user_id: int, event: dict):
    page = max(1, int(get_query_param(event, "page", 1)))
    limit = min(50, max(1, int(get_query_param(event, "limit", 10))))
    offset = (page - 1) * limit

    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS total FROM `order` WHERE users_id = %s", (user_id,))
        total_items = cur.fetchone()["total"]

        cur.execute(
            "SELECT carts_id FROM `order` WHERE users_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (user_id, limit, offset),
        )
        order_ids = [r["carts_id"] for r in cur.fetchall()]

    orders = [_build_order_payload(user_id, oid) for oid in order_ids]
    return response(200, {
        "data": orders,
        "meta": {
            "page": page,
            "limit": limit,
            "total_items": total_items,
            "total_pages": max(1, math.ceil(total_items / limit)),
        },
    })


def handle_get_order(user_id: int, order_id: int, status_code: int = 200):
    payload = _build_order_payload(user_id, order_id)
    if not payload:
        return error_response(404, "ORDER_NOT_FOUND", "Orden no encontrada.")
    return response(status_code, payload)


def _build_order_payload(user_id: int, order_id: int) -> dict | None:
    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cur.execute(
            "SELECT * FROM `order` WHERE carts_id = %s AND users_id = %s",
            (order_id, user_id),
        )
        order = cur.fetchone()
        if not order:
            return None

        cur.execute("SELECT * FROM order_items WHERE order_carts_id = %s", (order_id,))
        raw_items = cur.fetchall()

        cur.execute(
            """
            SELECT s.id, s.sellers_id AS seller_id, s.tracking_number, sm.carrier,
                   s.status, s.shipping_cost, s.shipped_at, s.estimated_delivered_at, s.delivered_at
            FROM shipments s
            JOIN shipping_methods sm ON s.shipping_methods_id = sm.id
            WHERE s.order_carts_id = %s
            """,
            (order_id,),
        )
        shipments = cur.fetchall()

    prod_ids = list({i["products_id"] for i in raw_items})
    prods_map = {}
    if prod_ids:
        placeholders = ",".join(["%s"] * len(prod_ids))
        conn_cat = get_connection("db_catalog")
        with conn_cat.cursor() as cur_cat:
            cur_cat.execute(
                f"SELECT id, public_id, name FROM products WHERE id IN ({placeholders})",
                tuple(prod_ids),
            )
            prods_map = {r["id"]: r for r in cur_cat.fetchall()}

    items = []
    for oi in raw_items:
        p = prods_map.get(oi["products_id"], {})
        sel_attrs = oi.get("selected_attributes")
        if isinstance(sel_attrs, str):
            sel_attrs = json.loads(sel_attrs)
        items.append({
            "order_item_id": oi["id"],
            "product_public_id": str(uuid.UUID(bytes=p["public_id"])) if p.get("public_id") else None,
            "product_name": p.get("name", "Producto"),
            "quantity": oi["quantity"],
            "unit_price": oi["unit_price"],
            "final_price": oi["final_price"],
            "selected_attributes": sel_attrs,
        })

    for s in shipments:
        if not s["tracking_number"]:
            s["tracking_number"] = None

    return {
        "order_id": order["carts_id"],
        "status": order["status"],
        "subtotal": order["subtotal"],
        "discount_total": order["discount_total"],
        "shipping_total": order["shipping_total"],
        "total": order["total"],
        "created_at": order["created_at"],
        "items": items,
        "shipments": shipments,
    }
