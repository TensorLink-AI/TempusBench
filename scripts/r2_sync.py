#!/usr/bin/env python3
"""
Upload/download TempusBench task datasets to/from Cloudflare R2.

Syncs the benchmark's task data (the time-series CSVs and task.yaml configs)
so they can be distributed and consumed from R2.

Usage:
    # Upload all tasks
    python scripts/r2_sync.py upload

    # Upload only multivariate tasks
    python scripts/r2_sync.py upload --filter multivariate

    # Upload a single task
    python scripts/r2_sync.py upload --filter univariate/absent_binary_univariate

    # List what's in the bucket
    python scripts/r2_sync.py list

    # Download tasks from R2
    python scripts/r2_sync.py download ./local_tasks/

Environment variables (override settings.yaml):
    R2_BUCKET             - Bucket name
    R2_ENDPOINT_URL       - S3-compatible endpoint
    R2_ACCESS_KEY_ID      - Access key
    R2_SECRET_ACCESS_KEY  - Secret key
    R2_PREFIX             - Key prefix (default: "tempusbench")
"""

import argparse
import sys
from pathlib import Path

# Add project root to path so we can import tempus_bench modules
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from tempus_bench.utils.r2_client import R2StorageClient


TASKS_DIR = project_root / "tempus_bench" / "tasks"


def load_r2_config():
    """Load R2 credentials from r2.yaml if it exists."""
    import yaml

    r2_path = project_root / "tempus_bench" / "config" / "r2.yaml"
    if not r2_path.exists():
        return {}
    with open(r2_path, "r") as f:
        return yaml.safe_load(f) or {}


def create_client(args) -> R2StorageClient:
    """Create an R2StorageClient from CLI args, env vars, or settings.yaml."""
    settings = load_r2_config()

    client = R2StorageClient(
        enabled=True,
        bucket=args.bucket or settings.get("r2_bucket", ""),
        endpoint_url=args.endpoint or settings.get("r2_endpoint_url", ""),
        access_key_id=args.access_key or settings.get("r2_access_key_id", ""),
        secret_access_key=args.secret_key or settings.get("r2_secret_access_key", ""),
        prefix=args.prefix or settings.get("r2_prefix", "tempusbench"),
    )

    if not client.enabled:
        print(
            "Error: R2 client could not be initialized. Check credentials.\n"
            "Set via env vars (R2_BUCKET, R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY) or in tempus_bench/config/settings.yaml"
        )
        sys.exit(1)

    return client


def collect_task_files(filter_path: str = "") -> list:
    """
    Collect all task files (CSVs + task.yaml) under the tasks directory.

    Args:
        filter_path: Optional sub-path filter, e.g. "multivariate" or
                     "univariate/absent_binary_univariate"

    Returns:
        List of (local_path, r2_key) tuples. R2 keys mirror the directory
        structure: tasks/univariate/task_name/file.csv
    """
    search_dir = TASKS_DIR / filter_path if filter_path else TASKS_DIR
    if not search_dir.exists():
        print(f"Error: {search_dir} does not exist")
        sys.exit(1)

    files = []
    for path in sorted(search_dir.rglob("*")):
        if not path.is_file():
            continue
        # Only upload CSVs and YAML configs
        if path.suffix not in (".csv", ".yaml", ".yml"):
            continue
        r2_key = f"tasks/{path.relative_to(TASKS_DIR)}"
        files.append((path, r2_key))

    return files


def cmd_upload(args):
    """Upload task datasets to R2."""
    client = create_client(args)
    files = collect_task_files(args.filter)

    if not files:
        print("No task files found to upload")
        sys.exit(1)

    print(f"Uploading {len(files)} task file(s) to R2 (prefix: {client.prefix}):\n")
    uploaded = 0
    for local_path, r2_key in files:
        ok = client.upload_file(str(local_path), r2_key)
        status = "ok" if ok else "FAILED"
        print(f"  {r2_key}  [{status}]")
        if ok:
            uploaded += 1

    print(f"\n{uploaded}/{len(files)} files uploaded")


def cmd_download(args):
    """Download task datasets from R2 to local disk."""
    client = create_client(args)
    local_dir = Path(args.local_dir).resolve()

    keys = client.list_objects("tasks/")
    if not keys:
        print("No task data found in R2")
        return

    local_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(keys)} file(s) to {local_dir}/:\n")

    downloaded = 0
    for key in keys:
        # Strip the bucket prefix to get the relative path (tasks/univariate/...)
        if client.prefix:
            relative = key[len(client.prefix) :].lstrip("/")
        else:
            relative = key
        local_file = local_dir / relative
        try:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            client._client.download_file(client.bucket, key, str(local_file))
            downloaded += 1
            print(f"  {relative}")
        except Exception as e:
            print(f"  FAILED: {relative} ({e})")

    print(f"\n{downloaded}/{len(keys)} files downloaded")


def cmd_list(args):
    """List task data in the R2 bucket."""
    client = create_client(args)

    keys = client.list_objects("tasks/")
    if not keys:
        print("No task data found")
        return

    print(f"Task data in s3://{client.bucket}/{client.prefix}/tasks/:\n")
    for key in keys:
        # Show just the relative path
        if client.prefix:
            relative = key[len(client.prefix) :].lstrip("/")
        else:
            relative = key
        print(f"  {relative}")
    print(f"\n{len(keys)} objects total")


def main():
    parser = argparse.ArgumentParser(
        description="Upload/download TempusBench task datasets to/from R2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # R2 credential overrides
    parser.add_argument("--bucket", default="", help="R2 bucket name")
    parser.add_argument("--endpoint", default="", help="R2 endpoint URL")
    parser.add_argument("--access-key", default="", help="R2 access key ID")
    parser.add_argument("--secret-key", default="", help="R2 secret access key")
    parser.add_argument("--prefix", default="", help="R2 key prefix")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # upload
    upload_parser = subparsers.add_parser(
        "upload", help="Upload task datasets to R2"
    )
    upload_parser.add_argument(
        "--filter",
        default="",
        help="Sub-path filter, e.g. 'multivariate' or 'univariate/absent_binary_univariate'",
    )

    # download
    download_parser = subparsers.add_parser(
        "download", help="Download task datasets from R2"
    )
    download_parser.add_argument(
        "local_dir",
        nargs="?",
        default=".",
        help="Local directory to save files (default: current directory)",
    )

    # list
    subparsers.add_parser("list", help="List task data in R2")

    args = parser.parse_args()

    if args.command == "upload":
        cmd_upload(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
