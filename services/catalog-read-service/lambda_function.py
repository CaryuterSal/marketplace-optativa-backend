import math
import uuid
from decimal import Decimal
from boto3.dynamodb.conditions import Attr
from shared.db_mysql import get_connection
from shared.db_dynamo import get_table
from shared.http_responses import response, error_response, get_path_param, get_query_param


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath") or event.get("path", "")

    try:
        if method == "GET" and path.endswith("/categories"):
            return handle_get_categories()
        if method == "GET" and path.endswith("/tags/trending"):
            return handle_trending_tags(event)
        if method == "GET" and path.endswith("/products"):
            return handle_list_products(event)
        if method == "GET" and "/products/" in path:
            public_id = get_path_param(event, "public_id") or path.rstrip("/").split("/")[-1]
            return handle_get_product_detail(public_id)
        return error_response(404, "ROUTE_NOT_FOUND", f"Ruta no soportada: {method} {path}")
    except Exception as e:
        return error_response(500, "INTERNAL_ERROR", str(e))


def handle_get_categories():
    table = get_table("DYNAMO_CATEGORIES_TABLE", "Categories")
    items = table.scan(FilterExpression=Attr("IsActive").eq(True)).get("Items", [])

    nodes = {}
    for it in items:
        nodes[it["CategoryID"]] = {
            "category_id": it["CategoryID"],
            "name": it["Name"],
            "slug": it["Slug"],
            "parent_category_id": it.get("ParentCategoryID"),
            "description": it.get("Description", ""),
            "is_active": bool(it.get("IsActive", True)),
            "children": [],
        }

    roots = []
    for node in nodes.values():
        parent_id = node["parent_category_id"]
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            roots.append(node)
    return response(200, roots)


def handle_trending_tags(event):
    limit = int(get_query_param(event, "limit", 10))
    table = get_table("DYNAMO_TAGS_TABLE", "Tags")
    items = table.scan().get("Items", [])
    sorted_items = sorted(items, key=lambda x: int(x.get("UsageCount", 0)), reverse=True)[:limit]

    return response(200, [
        {
            "tag_id": it["TagID"],
            "name": it["Name"],
            "slug": it["Slug"],
            "usage_count": int(it.get("UsageCount", 0)),
        }
        for it in sorted_items
    ])


def handle_list_products(event):
    page = max(1, int(get_query_param(event, "page", 1)))
    limit = min(100, max(1, int(get_query_param(event, "limit", 20))))
    q = (get_query_param(event, "q") or "").strip()
    min_price = get_query_param(event, "min_price")
    max_price = get_query_param(event, "max_price")
    category_id = get_query_param(event, "category_id")
    tag = get_query_param(event, "tag")

    where_clauses = ["p.deleted_at IS NULL"]
    params = []

    if q:
        where_clauses.append("p.name LIKE %s")
        params.append(f"%{q}%")
    if min_price is not None:
        where_clauses.append("p.price >= %s")
        params.append(float(min_price))
    if max_price is not None:
        where_clauses.append("p.price <= %s")
        params.append(float(max_price))

    where_sql = " AND ".join(where_clauses)
    offset = (page - 1) * limit

    conn = get_connection("db_catalog")
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS total FROM products p WHERE {where_sql}", tuple(params))
        total_items = cur.fetchone()["total"]

        cur.execute(
            f"""
            SELECT p.id, p.public_id, p.name, p.sellers_id, p.price, p.created_at,
                   COALESCE(SUM(i.stock_quantity), 0) AS stock_available
            FROM products p
            LEFT JOIN inventory i ON i.products_id = p.id
            WHERE {where_sql}
            GROUP BY p.id
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [limit, offset]),
        )
        sql_products = cur.fetchall()

        product_ids = [r["id"] for r in sql_products]
        discounts_map = _fetch_active_discounts(cur, product_ids)

    dynamo_table = get_table("DYNAMO_PRODUCTS_TABLE", "Products")
    results = []
    for row in sql_products:
        pub_uuid = str(uuid.UUID(bytes=row["public_id"]))
        dyn_item = dynamo_table.get_item(Key={"ProductID": pub_uuid}).get("Item", {})

        if category_id and dyn_item.get("CategoryID") != category_id:
            continue
        if tag and tag not in dyn_item.get("Tags", []):
            continue

        results.append(_merge_product_summary(row, pub_uuid, dyn_item, discounts_map.get(row["id"])))

    return response(200, {
        "data": results,
        "meta": {
            "page": page,
            "limit": limit,
            "total_items": total_items,
            "total_pages": max(1, math.ceil(total_items / limit)),
        },
    })


def handle_get_product_detail(public_id: str):
    try:
        uuid_bytes = uuid.UUID(public_id).bytes
    except ValueError:
        return error_response(400, "INVALID_UUID", "public_id debe ser un UUID válido.")

    conn = get_connection("db_catalog")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.public_id, p.name, p.sellers_id, p.price, p.created_at,
                   COALESCE(SUM(i.stock_quantity), 0) AS stock_available
            FROM products p
            LEFT JOIN inventory i ON i.products_id = p.id
            WHERE p.public_id = %s AND p.deleted_at IS NULL
            GROUP BY p.id
            """,
            (uuid_bytes,),
        )
        row = cur.fetchone()
        if not row:
            return error_response(404, "PRODUCT_NOT_FOUND", "Producto no encontrado.")
        discounts_map = _fetch_active_discounts(cur, [row["id"]])

    dynamo_table = get_table("DYNAMO_PRODUCTS_TABLE", "Products")
    dyn_item = dynamo_table.get_item(Key={"ProductID": public_id}).get("Item", {})

    summary = _merge_product_summary(row, public_id, dyn_item, discounts_map.get(row["id"]))
    summary["attributes"] = dyn_item.get("Attributes", {})
    summary["created_at"] = row["created_at"]
    return response(200, summary)


def _fetch_active_discounts(cur, product_ids: list) -> dict:
    if not product_ids:
        return {}
    placeholders = ",".join(["%s"] * len(product_ids))
    cur.execute(
        f"""
        SELECT products_id, title, discount_type, discount_value, end_date
        FROM discounts
        WHERE products_id IN ({placeholders})
          AND is_active = 1
          AND start_date <= NOW()
          AND end_date >= NOW()
        """,
        tuple(product_ids),
    )
    return {r["products_id"]: r for r in cur.fetchall()}


def _merge_product_summary(row: dict, pub_uuid: str, dyn_item: dict, discount: dict | None) -> dict:
    base_price = float(row["price"])
    final_price = base_price
    active_discount = None

    if discount:
        d_val = float(discount["discount_value"])
        if discount["discount_type"] == "PERCENTAGE":
            final_price = round(base_price * (1 - d_val / 100.0), 2)
        else:
            final_price = max(0.0, round(base_price - d_val, 2))
        active_discount = {
            "title": discount["title"],
            "discount_type": discount["discount_type"],
            "discount_value": d_val,
            "final_price": final_price,
            "ends_at": discount["end_date"],
        }

    stock = int(row["stock_available"])
    status = dyn_item.get("Status", "ACTIVE")
    if stock <= 0 and status == "ACTIVE":
        status = "OUT_OF_STOCK"

    return {
        "public_id": pub_uuid,
        "name": row["name"],
        "category_id": dyn_item.get("CategoryID", "UNCATEGORIZED"),
        "seller_id": row["sellers_id"],
        "base_price": base_price,
        "final_price": final_price,
        "active_discount": active_discount,
        "stock_available": stock,
        "status": status,
        "images": dyn_item.get("Images", []),
        "tags": dyn_item.get("Tags", []),
    }
