from __future__ import annotations

import pytest

from agent_evals.entities import normalize_url, numbers_in, urls_in


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Bucket names must be between 3 (min) and 63 (max) characters long.", {"3", "63"}),
        ("up to 10,240 MB of memory", {"10240"}),
        ("large payload sizes (up to 1GB) and videos up to 10GB in size", {"1", "10"}),
        ("S3, EC2, python3.10, java17, AL2023, IMDSv2 and m5.large are names", set()),
        ("ml.m5.4xlarge and 2xlarge are instance sizes", set()),
        ("10.0.0.0/16 is a CIDR block", {"16"}),
        ("us-east-1, us-west-2 and eu-west-1a are places", set()),
        ("99.9% durability, 0.5 ACUs, $29/month, 24/7", {"99.9", "0.5", "29", "24", "7"}),
        ("deprecated on Oct 31, 2026.", {"31", "2026"}),
        ("version 1.2.3 of the SDK", set()),
        ("Steps:\n1. Count them.\n2) Multiply by 4 KB.\n 10. Done", {"4"}),
    ],
)
def test_numbers_in(text: str, expected: set[str]) -> None:
    assert numbers_in(text) == expected


def test_urls_are_normalized() -> None:
    text = (
        "See https://docs.aws.amazon.com/lambda/latest/dg/limits.html#timeout, "
        "(https://Docs.AWS.amazon.com/lambda/latest/dg/limits.html/) and http://example.test/a."
    )
    assert urls_in(text) == {
        "https://docs.aws.amazon.com/lambda/latest/dg/limits.html",
        "http://example.test/a",
    }
    assert normalize_url("HTTPS://X.test/p/#frag") == "https://x.test/p"
