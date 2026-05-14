"""Shared fixtures: a moto-backed S3 bucket the rest of the suite can use."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from einkgen.core import s3 as s3mod

TEST_BUCKET = "einkgen-test"
TEST_REGION = "us-east-1"
TEST_CDN_BASE = "https://cdn.test.example.com"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", TEST_REGION)
    monkeypatch.setenv("EINKGEN_BUCKET", TEST_BUCKET)
    monkeypatch.setenv("EINKGEN_CDN_BASE", TEST_CDN_BASE)
    monkeypatch.delenv("EINKGEN_CF_DISTRIBUTION_ID", raising=False)
    yield


@pytest.fixture
def s3_bucket(aws_env):
    with mock_aws():
        client = boto3.client("s3", region_name=TEST_REGION)
        client.create_bucket(Bucket=TEST_BUCKET)
        s3mod.reset_client()
        try:
            yield client
        finally:
            s3mod.reset_client()
