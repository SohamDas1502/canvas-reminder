"""Upload the website to the web stack's S3 bucket and refresh CloudFront.

    make web-publish      (runs automatically after `make web-deploy`)

config.json is generated from the stack outputs, so nothing environment-specific is
committed to the repo.
"""

from __future__ import annotations

import json
import mimetypes
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "web" / "site"
STACK = "canvas-reminder-web"
REGION = "ap-southeast-2"
# HTML and config must update immediately; static assets can be cached briefly.
NO_CACHE = {".html", ".json", ".js", ".css"}


def main() -> None:
    stack = boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName=STACK)
    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack["Stacks"][0]["Outputs"]}
    s3 = boto3.client("s3", region_name=REGION)
    bucket = outputs["SiteBucket"]

    config = {"region": outputs["Region"], "userPoolClientId": outputs["UserPoolClientId"]}
    s3.put_object(
        Bucket=bucket,
        Key="config.json",
        Body=json.dumps(config).encode(),
        ContentType="application/json",
        CacheControl="no-cache",
    )
    for path in sorted(SITE.rglob("*")):
        if path.is_dir() or path.name.startswith("."):
            continue
        key = path.relative_to(SITE).as_posix()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=path.read_bytes(),
            ContentType=f"{content_type}; charset=utf-8"
            if content_type.startswith("text/")
            else content_type,
            CacheControl="no-cache" if path.suffix in NO_CACHE else "public, max-age=86400",
        )
        print(f"  uploaded {key}")

    boto3.client("cloudfront").create_invalidation(
        DistributionId=outputs["DistributionId"],
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": str(time.time()),
        },
    )
    print(f"\nPublished. Open {outputs['SiteUrl']}")


if __name__ == "__main__":
    main()
