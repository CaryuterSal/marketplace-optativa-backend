import os
import boto3

_dynamodb = None


def get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )
    return _dynamodb


def get_table(env_var_name: str, default_table_name: str):
    table_name = os.environ.get(env_var_name, default_table_name)
    return get_dynamodb().Table(table_name)
