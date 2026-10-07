"""Ship the built StorageLens SPA to its S3 hosting bucket.

The stack's template pins the frontend origin to a CloudFront
distribution that reads from a private S3 bucket. This script builds
the SPA, uploads `frontend/dist` to the stack's `FrontendBucket`, and
invalidates the distribution's cache when it exists.

    python scripts/deploy_frontend.py [--stack-name NAME] [--region ap-south-1]

Defaults match the other scripts: the standard stack name and the
region the session credentials resolve to. Use `--skip-build` to
upload an already-built `frontend/dist` unchanged (e.g. right after
`npm run build` in a shell whose npm PATH the script cannot see).

If the stack has no CloudFront distribution yet (restore excluded it
while the AWS account was pending CloudFront verification), the files
still land in the bucket and the script exits explaining what is
missing — nothing is cached wrong because nothing is serving yet.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import boto3

STACK_NAME = "intelligent-storage-cost-optimizer"
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"
REQUIRED_OUTPUT = "FrontendBucketName"
DISTRIBUTION_OUTPUT = "FrontendDistributionDomain"

# Content-Type for every extension found in a Vite build; anything
# else rides as application/octet-stream (browsers still download it,
# they just cannot display it inline).
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}

# Vite fingerprints files under /assets by content hash, so those
# uploads are immutable; the entry HTML and public files can change
# between deploys with unchanged names, so they get no-cache.
IMMUTABLE_PREFIX = "assets/"


def content_type_and_cache(key):
    ext = Path(key).suffix.lower()
    content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
    if key.startswith(IMMUTABLE_PREFIX):
        cache_control = "public, max-age=31536000, immutable"
    elif ext == ".html":
        # The SPA shell must revalidate — it references the hashed
        # asset names, so a cached shell can miss a new deploy.
        cache_control = "no-cache"
    else:
        cache_control = "public, max-age=300"
    return content_type, cache_control


def collect_outputs(stack_name, region):
    """Stack outputs as {OutputKey: OutputValue}, failing loudly if the
    stack or the hosting bucket output is missing."""
    cfn = boto3.client("cloudformation", region_name=region)
    response = cfn.describe_stacks(StackName=stack_name)
    outputs = response["Stacks"][0].get("Outputs", [])
    mapping = {o["OutputKey"]: o["OutputValue"] for o in outputs}

    if REQUIRED_OUTPUT not in mapping:
        raise SystemExit(
            f"Stack '{stack_name}' has no '{REQUIRED_OUTPUT}' output — "
            "this script expects a deployed StorageLens stack."
        )
    return mapping


def build_frontend(frontend_dir):
    """Run `npm run build` (tsc + vite), pointing at the real npm on
    this platform; raise if the build fails so nothing stale uploads."""
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit(
            "'npm' not found on PATH — build frontend/ yourself and rerun "
            "with --skip-build."
        )
    result = subprocess.run(
        [npm, "run", "build"],
        cwd=frontend_dir,
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"`npm run build` failed with exit code {result.returncode}.")
    return result.stdout


def collect_upload_files(dist_dir):
    """(local_path, key) pairs for every file under dist, with keys like
    'index.html' and 'assets/index-abc123.js'."""
    dist_dir = Path(dist_dir)
    if not (dist_dir / "index.html").is_file():
        raise SystemExit(
            f"{dist_dir} has no index.html — build first (omit --skip-build) "
            "or point the script at the right dist directory."
        )
    files = [
        (path, path.relative_to(dist_dir).as_posix())
        for path in sorted(dist_dir.rglob("*"))
        if path.is_file()
    ]
    if not files:
        raise SystemExit(f"{dist_dir} contains no files to upload.")
    return files


def upload_dist(dist_dir, bucket_name, s3):
    """Upload every dist file, remembering previously-live keys so they
    can be pruned after; returns (uploaded_keys, prune_pool)."""
    files = collect_upload_files(dist_dir)

    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket_name)
    live_keys = {
        item["Key"] for page in pages for item in page.get("Contents", [])
    }

    uploaded_keys = set()
    for local_path, key in files:
        content_type, cache_control = content_type_and_cache(key)
        s3.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=local_path.read_bytes(),
            ContentType=content_type,
            CacheControl=cache_control,
        )
        uploaded_keys.add(key)

    # Keys live on S3 but absent from this build are leftovers of an
    # old deploy (e.g. a previous asset hash).
    prune_keys = live_keys - uploaded_keys
    for key in sorted(prune_keys):
        s3.delete_object(Bucket=bucket_name, Key=key)
    return sorted(uploaded_keys), sorted(prune_keys)


def find_distribution(stack_name, region):
    """The stack distribution's id, or None when the restore excluded
    it (CloudFront account verification pending)."""
    cfn = boto3.client("cloudformation", region_name=region)
    response = cfn.list_stack_resources(StackName=stack_name)
    for resource in response["StackResourceSummaries"]:
        if resource["LogicalResourceId"] == "FrontendDistribution":
            return resource["PhysicalResourceId"]
    return None


def invalidate_cache(distribution_id, cloudfront=None):
    """Invalidate `/*` on the distribution so the SPA update is visible
    immediately; returns the invalidation id."""
    cloudfront = cloudfront or boto3.client("cloudfront")
    response = cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": __import__("uuid").uuid4().hex,
        },
    )
    return response["Invalidation"]["Id"]


def main():
    parser = argparse.ArgumentParser(
        description="Build the SPA and ship frontend/dist to the stack's "
        "hosting bucket (invalidating CloudFront when it exists)."
    )
    parser.add_argument("--stack-name", default=STACK_NAME)
    parser.add_argument("--region", default=None)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if not args.region:
        args.region = boto3.session.Session().region_name

    mapping = collect_outputs(args.stack_name, args.region)
    bucket_name = mapping[REQUIRED_OUTPUT]
    s3 = boto3.client("s3", region_name=args.region)

    if not args.skip_build:
        out = build_frontend(FRONTEND_DIR)
        print(out.strip())

    uploaded, pruned = upload_dist(FRONTEND_DIR / "dist", bucket_name, s3)
    print(f"Uploaded {len(uploaded)} files to s3://{bucket_name}/")
    for key in uploaded[:10]:
        print(f"  {key}")
    if len(uploaded) > 10:
        print(f"  … and {len(uploaded) - 10} more")
    if pruned:
        print(f"Deleted {len(pruned)} stale files:")
        for key in pruned[:10]:
            print(f"  {key}")
        if len(pruned) > 10:
            print(f"  … and {len(pruned) - 10} more")

    domain = mapping.get(DISTRIBUTION_OUTPUT)
    if domain:
        distribution_id = find_distribution(args.stack_name, args.region)
        if distribution_id:
            invalidation_id = invalidate_cache(
                distribution_id,
                boto3.client("cloudfront", region_name=args.region),
            )
            print(f"Invalidated CloudFront ({invalidation_id}); a minute or"
                  " two until it applies everywhere.")
        else:
            print("Distribution output present but the resource is not in"
                  " the stack — skip invalidation.")
        print(f"Serving at: https://{domain}/")
    else:
        print(
            "\nNOTE: the stack has no CloudFront distribution yet "
            f"(no '{DISTRIBUTION_OUTPUT}' output) — the files are in the "
            "bucket but nothing serves them. Add the distribution once"
            " your AWS account is CloudFront-verified, then redeploy the "
            "stack and rerun this script."
        )


if __name__ == "__main__":
    main()