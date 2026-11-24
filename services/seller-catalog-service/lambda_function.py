import uuid
from datetime import datetime, timezone
from decimal import Decimal
from shared.db_mysql import get_connection
from shared.db_dynamo import get_table
from shared.jwt_auth import require_auth
from shared.http_responses import response, error_response, parse_body, get_path_param


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        auth = require_auth(event)
        if method == "POST" and path.endswith("/sellers/onboarding"):
            return handle_seller_onboarding(auth["user_id"], event)
        if path.endswith("/sellers/me/warehouses"):
            if method == "GET":
                return handle_list_warehouses(auth["user_id"])
            if method == "POST":
                return handle_create_warehouse(auth["user_id"], event)
        if method == "POST" and path.endswith("/sellers/me/products"):
            return handle_create_product(auth["user_id"], event)
        if method == "PATCH" and path.endswith("/price"):
            public_id = get_path_param(event, "public_id") or path.split("/")[-2]
            return handle_update_price(auth["user_id"], public_id, event)
        if method == "POST" and path.endswith("/discounts"):
            public_id = get_path_param(event, "public_id") or path.split("/")[-2]
            return handle_create_discount(auth["user_id"], public_id, event)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except PermissionError as e:
        return error_response(401, "UNAUTHORIZED", str(e))
    except ValueError as e:
        return error_response(400, "VALIDATION_ERROR", str(e))
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def _get_seller_id(user_id: int) -> int:
    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM sellers WHERE users_id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            raise PermissionError("Debes completar el registro de vendedor (/sellers/onboarding).")
        return row["id"]


def handle_seller_onboarding(user_id: int, event: dict):
    body = parse_body(event)
    rfc = (body.get("rfc") or "").upper().strip()
    if not rfc:
        raise ValueError("El campo 'rfc' es obligatorio.")

    conn = get_connection("db_commerce")
    with conn.cursor() as cur:
        cur.execute("SELECT id, rfc FROM sellers WHERE users_id = %s", (user_id,))
        existing = cur.fetchone()
        if existing:
            return response(200, {"seller_id": existing["id"], "rfc": existing["rfc"]})

        cur.execute("INSERT INTO sellers (rfc, users_id) VALUES (%s, %s)", (rfc, user_id))
        seller_id = cur.lastrowid
    conn.commit()
    return response(201, {"seller_id": seller_id, "rfc": rfc})


def handle_list_warehouses(user_id: int):
    _get_seller_id(user_id)
    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cur.execute("SELECT id FROM address WHERE users_id = %s", (user_id,))
        addr_ids = [r["id"] for r in cur.fetchall()]

    if not addr_ids:
        return response(200, [])

    placeholders = ",".join(["%s"] * len(addr_ids))
    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute(
            f"SELECT id, name, address_id FROM warehouses WHERE address_id IN ({placeholders})",
            tuple(addr_ids),
        )
        rows = cur.fetchall()
    return response(200, rows)


def handle_create_warehouse(user_id: int, event: dict):
    _get_seller_id(user_id)
    body = parse_body(event)
    name = (body.get("name") or "").strip()
    address_id = body.get("address_id")
    if not name or not address_id:
        raise ValueError("name y address_id son obligatorios.")

    conn_comm = get_connection("db_commerce")
    with conn_comm.cursor() as cur:
        cur.execute("SELECT id FROM address WHERE id = %s AND users_id = %s", (address_id, user_id))
        if not cur.fetchone():
            raise ValueError("address_id no pertenece al usuario.")

    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute(
            "INSERT INTO warehouses (address_id, name) VALUES (%s, %s)",
            (address_id, name),
        )
        wh_id = cur.lastrowid
    conn_cat.commit()
    return response(201, {"id": wh_id, "name": name, "address_id": address_id})


def handle_create_product(user_id: int, event: dict):
    seller_id = _get_seller_id(user_id)
    body = parse_body(event)
    required = ["name", "category_id", "price", "initial_stock", "warehouse_id", "images", "attributes"]
    for req in required:
        if body.get(req) is None:
            raise ValueError(f"El campo '{req}' es obligatorio.")

    pub_uuid = uuid.uuid4()
    pub_uuid_str = str(pub_uuid)
    price = float(body["price"])
    stock = int(body["initial_stock"])
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Escritura transaccional en MySQL db_catalog
    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute("SELECT id FROM categories LIMIT 1")
        cat_row = cur.fetchone()
        if not cat_row:
            cur.execute("INSERT INTO categories (name) VALUES ('General')")
            sql_cat_id = cur.lastrowid
        else:
            sql_cat_id = cat_row["id"]

        cur.execute(
            """
            INSERT INTO products (public_id, name, categories_id, created_at, sellers_id, price)
            VALUES (%s, %s, %s, NOW(), %s, %s)
            """,
            (pub_uuid.bytes, body["name"], sql_cat_id, seller_id, price),
        )
        prod_id = cur.lastrowid

        cur.execute(
            "INSERT INTO product_revisions (revision_date, products_id, price) VALUES (NOW(), %s, %s)",
            (prod_id, price),
        )

        inv_id = f"INV-{uuid.uuid4().hex[:12]}"
        cur.execute(
            "INSERT INTO inventory (id, warehouses_id, products_id, stock_quantity) VALUES (%s, %s, %s, %s)",
            (inv_id, body["warehouse_id"], prod_id, stock),
        )
    conn_cat.commit()

    # 2. Dual-Write en DynamoDB Products (atributos dinámicos e imágenes)
    dynamo_prods = get_table("DYNAMO_PRODUCTS_TABLE", "Products")
    dynamo_prods.put_item(Item={
        "ProductID": pub_uuid_str,
        "SellerID": f"SELL-{seller_id}",
        "Name": body["name"],
        "CategoryID": body["category_id"],
        "Price": Decimal(str(price)),
        "Status": "ACTIVE" if stock > 0 else "OUT_OF_STOCK",
        "Tags": body.get("tags", []),
        "Images": body["images"],
        "Attributes": body["attributes"],
        "CreatedAt": now_iso,
    })

    return response(201, {
        "public_id": pub_uuid_str,
        "name": body["name"],
        "category_id": body["category_id"],
        "seller_id": seller_id,
        "base_price": price,
        "final_price": price,
        "active_discount": None,
        "stock_available": stock,
        "status": "ACTIVE" if stock > 0 else "OUT_OF_STOCK",
        "images": body["images"],
        "tags": body.get("tags", []),
        "attributes": body["attributes"],
        "created_at": now_iso,
    })


def handle_update_price(user_id: int, public_id: str, event: dict):
    seller_id = _get_seller_id(user_id)
    body = parse_body(event)
    new_price = float(body.get("price", 0))
    if new_price <= 0:
        raise ValueError("price debe ser mayor a 0.")

    uuid_bytes = uuid.UUID(public_id).bytes
    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute(
            "SELECT id FROM products WHERE public_id = %s AND sellers_id = %s AND deleted_at IS NULL",
            (uuid_bytes, seller_id),
        )
        prod = cur.fetchone()
        if not prod:
            return error_response(404, "PRODUCT_NOT_FOUND", "Producto no encontrado para este vendedor.")

        cur.execute("UPDATE products SET price = %s WHERE id = %s", (new_price, prod["id"]))
        cur.execute(
            "INSERT INTO product_revisions (revision_date, products_id, price) VALUES (NOW(), %s, %s)",
            (prod["id"], new_price),
        )
    conn_cat.commit()

    dynamo_prods = get_table("DYNAMO_PRODUCTS_TABLE", "Products")
    dynamo_prods.update_item(
        Key={"ProductID": public_id},
        UpdateExpression="SET Price = :p",
        ExpressionAttributeValues={":p": Decimal(str(new_price))},
    )
    return response(200, {"public_id": public_id, "price": new_price})


def handle_create_discount(user_id: int, public_id: str, event: dict):
    seller_id = _get_seller_id(user_id)
    body = parse_body(event)
    required = ["title", "discount_type", "discount_value", "start_date", "end_date"]
    for req in required:
        if not body.get(req):
            raise ValueError(f"El campo '{req}' es obligatorio.")

    uuid_bytes = uuid.UUID(public_id).bytes
    conn_cat = get_connection("db_catalog")
    with conn_cat.cursor() as cur:
        cur.execute(
            "SELECT id FROM products WHERE public_id = %s AND sellers_id = %s AND deleted_at IS NULL",
            (uuid_bytes, seller_id),
        )
        prod = cur.fetchone()
        if not prod:
            return error_response(404, "PRODUCT_NOT_FOUND", "Producto no encontrado para este vendedor.")

        cur.execute(
            """
            INSERT INTO discounts (
                title, discount_type, discount_value, start_date, end_date, is_active, products_id
            )
            VALUES (%s, %s, %s, %s, %s, 1, %s)
            """,
            (
                body["title"], body["discount_type"], body["discount_value"],
                body["start_date"].replace("Z", ""), body["end_date"].replace("Z", ""),
                prod["id"],
            ),
        )
        disc_id = cur.lastrowid
    conn_cat.commit()
    return response(201, {"discount_id": disc_id, "product_public_id": public_id})
