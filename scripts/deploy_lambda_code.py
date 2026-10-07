"""Deploy one StorageLens lambda's code from its source directory.

Bypasses `sam deploy` (the template must stay distribution-less while
CloudFront account verification is pending) by zipping the lambda's
CodeUri directory, uploading it to the ingest-QA bucket under a
deploy/ prefix, and calling UpdateFunctionCode. Prints the new
CodeSha256 so the caller can confirm the update took.

Usage: python scripts/deploy_lambda_code.py decisions|oncall
     [--function FN] [--keep-zip]
"""

import argparse
import os
import sys
import time
import zipfile

import boto3

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LAMBDA_DIRS = {
    "decisions": os.path.join(REPO_ROOT, "backend", "lambdas", "decisions"),
    "oncall": os.path.join(REPO_ROOT, "backend", "lambdas", "oncall"),
}

BUNDLE_EXTS = (".py", ".json")

REGION = os.environ.get("AWS_REGION", "ap-south-1")
QA_BUCKET_ENV = "STORAGELENS_DEPLOY_BUCKET"


def find_function_name(client, logical_suffix):
    """Physical names carry the stack prefix plus the logical name."""
    for page in client.get_paginator("list_functions").paginate():
        for function in page["Functions"]:
            if f"-{logical_suffix}-" in function["FunctionName"]:
                return function["FunctionName"]
    raise SystemExit(
        f"no function found containing '-{logical_suffix}-'"
    )


def build_zip(source_dir, out_path):
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for root, dirs, _files in os.walk(source_dir):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in sorted(_files):
                if name.endswith(BUNDLE_EXTS):
                    path = os.path.join(root, name)
                    arcname = os.path.relpath(path, source_dir)
                    bundle.write(path, arcname.replace(os.sep, "/"))
                    count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("lambda_name", choices=sorted(LAMBDA_DIRS))
    parser.add_argument("--function", help="override physical name")
    parser.add_argument("--keep-zip", action="store_true")
    args = parser.parse_args()

    source_dir = LAMBDA_DIRS[args.lambda_name]
    # Logical suffixes: DecisionsFunction / OnCallFunction.
    suffix_map = {
        "decisions": "DecisionsFunction",
        "oncall": "OnCallFunction",
    }
    logical_suffix = suffix_map[args.lambda_name]

    session = boto3.session.Session(region_name=REGION)
    lam = session.client("lambda")
    s3 = session.client("s3")

    function_name = args.function or find_function_name(
        lam, logical_suffix
    )

    bundle = os.path.join(os.environ.get("TEMP", "/tmp"),
                          f"storagelens-{args.lambda_name}-deploy.zip")
    file_count = build_zip(source_dir, bundle)
    size = os.path.getsize(bundle)

    if os.environ.get(QA_BUCKET_ENV):
        bucket = os.environ[QA_BUCKET_ENV]
        upload_note = f"s3 upload ({size} bytes)"
    else:
        # Direct zip upload is capped at 50 MB uncompressed; these
        # bundles are tens of KB.
        bucket = None
        upload_note = f"zipfile upload ({size} bytes, {file_count} files)"

    if bucket:
        key = f"deploy/{os.path.basename(bundle)}"
        s3.upload_file(bundle, bucket, key,
                       ExtraArgs={"ContentType": "application/zip"})
        code = {"S3Bucket": bucket, "S3Key": key}
    else:
        with open(bundle, "rb") as fh:
            code = {"ZipFile": fh.read()}

    response = lam.update_function_code(FunctionName=function_name, **code)

    print(f"deploying {function_name} — {upload_note}")

    for _ in range(30):
        config = lam.get_function_configuration(
            FunctionName=function_name)
        state = config["LastUpdateStatus"]
        if state != "InProgress":
            break
        time.sleep(2)

    if bucket and not args.keep_zip:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as error:
            print(f"warn: could not delete deploy object {key}: {error}")
    if not args.keep_zip:
        try:
            os.remove(bundle)
        except OSError:
            pass

    if state != "Successful":
        print(f"FAILED: LastUpdateStatus={state} "
              f"{config.get('LastUpdateStatusReason') or ''}")
        return 1

    print(f"OK state={state} sha256={config['CodeSha256']} "
          f"version={response.get('Version')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())