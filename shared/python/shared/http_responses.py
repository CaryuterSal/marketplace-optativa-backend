import json
from decimal import Decimal
from datetime import datetime, date
import uuid


class CustomEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, bytes):
            return str(uuid.UUID(bytes=obj))
        return super().default(obj)


def response(status_code: int, body=None):
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,Authorization",
        "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    }
    if status_code == 204 or body is None:
        return {"statusCode": status_code, "headers": headers, "body": ""}
    return {
        "statusCode": status_code,
        "headers": headers,
        "body": json.dumps(body, cls=CustomEncoder),
    }


def error_response(status_code: int, code: str, message: str, details=None):
    payload = {"code": code, "message": message}
    if details:
        payload["details"] = details
    return response(status_code, payload)


def parse_body(event: dict) -> dict:
    body = event.get("body")
    if not body:
        return {}
    if isinstance(body, dict):
        return body
    return json.loads(body)


def get_path_param(event: dict, name: str):
    return (event.get("pathParameters") or {}).get(name)


def get_query_param(event: dict, name: str, default=None):
    return (event.get("queryStringParameters") or {}).get(name, default)
