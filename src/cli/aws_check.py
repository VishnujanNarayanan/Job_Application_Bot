"""CLI — verify AWS connectivity for the runtime user.

Usage:  python -m src.cli.aws_check

Exercises the minimal-permission set defined in CLAUDE.md hard rule #19:
  - S3: PutObject + GetObject + DeleteObject on AWS_S3_BUCKET
  - CloudWatch Logs: CreateLogStream + PutLogEvents on AWS_CLOUDWATCH_LOG_GROUP
  - CloudWatch Metrics: PutMetricData on AWS_CLOUDWATCH_NAMESPACE
  - (Negative) IAM operations MUST be denied — a runtime user that can
    call iam:ListUsers is over-privileged and violates hard rule #19.

Exit 0 if all pass, 1 if any fail. Intended for Iteration 0.1 verification
and as a smoke test after credential rotation.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv


def _env(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        print(f"FAIL  env var {key} is empty in .env")
        sys.exit(2)
    return val


def check_s3(bucket: str) -> bool:
    s3 = boto3.client("s3")
    key = f"_aws_check/{int(time.time())}.txt"
    body = b"aws_check probe"
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if got != body:
            print(f"FAIL  S3: GetObject returned {len(got)} bytes, expected {len(body)}")
            return False
        s3.delete_object(Bucket=bucket, Key=key)
    except ClientError as e:
        err = e.response["Error"]
        print(f"FAIL  S3: {err['Code']} — {err['Message']}")
        return False
    print(f"PASS  S3: PutObject + GetObject + DeleteObject on s3://{bucket}/")
    return True


def check_cloudwatch_logs(log_group: str) -> bool:
    logs = boto3.client("logs")
    stream = f"aws_check/{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    try:
        logs.create_log_stream(logGroupName=log_group, logStreamName=stream)
        logs.put_log_events(
            logGroupName=log_group,
            logStreamName=stream,
            logEvents=[{
                "timestamp": int(time.time() * 1000),
                "message": "aws_check probe",
            }],
        )
    except logs.exceptions.ResourceNotFoundException:
        print(
            f"FAIL  CloudWatch Logs: log group {log_group!r} does not exist. "
            f"Create it in AWS Console (suggested retention: 14 days)."
        )
        return False
    except ClientError as e:
        err = e.response["Error"]
        print(f"FAIL  CloudWatch Logs: {err['Code']} — {err['Message']}")
        return False
    print(f"PASS  CloudWatch Logs: CreateLogStream + PutLogEvents on {log_group}")
    return True


def check_cloudwatch_metrics(namespace: str) -> bool:
    cw = boto3.client("cloudwatch")
    try:
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[{
                "MetricName": "AwsCheckProbe",
                "Value": 0.0,
                "Unit": "Count",
                "Timestamp": datetime.now(timezone.utc),
            }],
        )
    except ClientError as e:
        err = e.response["Error"]
        print(f"FAIL  CloudWatch Metrics: {err['Code']} — {err['Message']}")
        return False
    print(f"PASS  CloudWatch Metrics: PutMetricData on namespace {namespace}")
    return True


def check_iam_denied() -> bool:
    iam = boto3.client("iam")
    try:
        iam.list_users()
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "AccessDeniedException"):
            print("PASS  IAM (negative): iam:ListUsers correctly denied")
            return True
        print(f"FAIL  IAM (negative): unexpected error {code}")
        return False
    print(
        "FAIL  IAM (negative): iam:ListUsers SUCCEEDED — runtime user is "
        "over-privileged, violates CLAUDE.md hard rule #19"
    )
    return False


def main() -> int:
    load_dotenv()

    region = _env("AWS_REGION")
    bucket = _env("AWS_S3_BUCKET")
    log_group = _env("AWS_CLOUDWATCH_LOG_GROUP")
    namespace = _env("AWS_CLOUDWATCH_NAMESPACE")

    if region != "ap-south-1":
        print(f"WARN  AWS_REGION is {region!r}, expected 'ap-south-1' "
              f"(CLAUDE.md hard rule: all resources in Mumbai)")

    print(f"Region:    {region}")
    print(f"Bucket:    {bucket}")
    print(f"Log group: {log_group}")
    print(f"Namespace: {namespace}")
    print()

    results = [
        check_s3(bucket),
        check_cloudwatch_logs(log_group),
        check_cloudwatch_metrics(namespace),
        check_iam_denied(),
    ]

    print()
    passed = results.count(True)
    total = len(results)
    if all(results):
        print(f"All {total} checks passed. AWS runtime is operational.")
        return 0
    print(f"{total - passed} of {total} checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
