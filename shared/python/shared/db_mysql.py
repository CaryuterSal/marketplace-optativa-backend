import os
import pymysql
from pymysql.cursors import DictCursor

_pools = {}


def get_connection(db_name: str):
    """
    Reutiliza la conexión TCP en warm starts de AWS Lambda.
    db_name debe ser 'db_commerce' o 'db_catalog'.
    """
    global _pools
    conn = _pools.get(db_name)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            return conn
        except Exception:
            _pools.pop(db_name, None)

    host = os.environ.get("RDS_PROXY_HOST") or os.environ["DB_HOST"]
    port = int(os.environ.get("DB_PORT", "3306"))
    user = os.environ["DB_USER"]
    password = os.environ["DB_PASSWORD"]

    conn = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db_name,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=False,
        connect_timeout=5,
    )
    _pools[db_name] = conn
    return conn
