#!/usr/bin/env python3
"""
Standalone script to upload/download TempusBench run results to/from Cloudflare R2.

This script operates independently from the benchmark pipeline — use it to push
existing local runs to R2, pull remote runs down, or list what's in the bucket.

Credentials are read from environment variables or from settings.yaml.

Usage:
    # Upload a specific run directory
    python scripts/r2_sync.py upload runs/run_20240101-120000

    # Upload all runs
    python scripts/r2_sync.py upload runs/

    # Upload a single file
    python scripts/r2_sync.py upload runs/run_20240101-120000/evals/evaluations.csv

    # List remote objects
    python scripts/r2_sync.py list
    python scripts/r2_sync.py list run_20240101-120000

    # Download a run
    python scripts/r2_sync.py download run_20240101-120000 ./local_output/

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


def load_settings_yaml():
    """Load R2 settings from settings.yaml if it exists."""
    import yaml

    settings_path = project_root / "tempus_bench" / "config" / "settings.yaml"
    if not settings_path.exists():
        return {}
    with open(settings_path, "r") as f:
        return yaml.safe_load(f) or {}


def create_client(args) -> R2StorageClient:
    """Create an R2StorageClient from CLI args, env vars, or settings.yaml."""
    settings = load_settings_yaml()

    client = R2StorageClient(
        enabled=True,  # Always enable for this script
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


def cmd_upload(args):
    """Upload a local file or directory to R2."""
    client = create_client(args)
    local_path = Path(args.path).resolve()

    if not local_path.exists():
        print(f"Error: {local_path} does not exist")
        sys.exit(1)

    if local_path.is_file():
        # Single file upload — derive key from path relative to project root
        try:
            relative = local_path.relative_to(project_root / "runs")
            r2_key = str(relative)
        except ValueError:
            r2_key = local_path.name

        print(f"Uploading {local_path} -> {r2_key}")
        if client.upload_file(str(local_path), r2_key):
            print("Done.")
        else:
            print("Upload failed.")
            sys.exit(1)

    elif local_path.is_dir():
        # Check if this is a single run dir or the runs/ parent
        children = [d for d in local_path.iterdir() if d.is_dir()]
        is_runs_parent = all(
            d.name.startswith("run_") for d in children
        ) and local_path.name == "runs"

        if is_runs_parent:
            # Upload each run subdirectory
            total = 0
            for run_dir in sorted(children):
                print(f"\nUploading {run_dir.name}/...")
                count = client.upload_directory(str(run_dir), run_dir.name)
                total += count
                print(f"  {count} files uploaded")
            print(f"\nTotal: {total} files uploaded across {len(children)} runs")
        else:
            # Single directory — use its name as the key prefix
            r2_prefix = local_path.name
            print(f"Uploading {local_path}/ -> {r2_prefix}/")
            count = client.upload_directory(str(local_path), r2_prefix)
            print(f"Done. {count} files uploaded.")


def cmd_download(args):
    """Download files from R2 to local disk."""
    client = create_client(args)
    r2_prefix = args.remote_path
    local_dir = Path(args.local_dir).resolve()

    print(f"Listing objects under: {r2_prefix}")
    keys = client.list_objects(r2_prefix)

    if not keys:
        print("No objects found.")
        return

    print(f"Found {len(keys)} objects. Downloading to {local_dir}/")
    downloaded = 0
    prefix_to_strip = client._make_key(r2_prefix)
    for key in keys:
        # Strip the full prefix to get the relative path
        relative = key
        if key.startswith(prefix_to_strip):
            relative = key[len(prefix_to_strip) :].lstrip("/")
        elif client.prefix and key.startswith(client.prefix):
            relative = key[len(client.prefix) :].lstrip("/")

        local_file = local_dir / relative
        # Use raw boto3 download since download_file prepends prefix
        try:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            client._client.download_file(client.bucket, key, str(local_file))
            downloaded += 1
            print(f"  {key} -> {local_file}")
        except Exception as e:
            print(f"  Failed: {key} ({e})")

    print(f"\nDone. {downloaded}/{len(keys)} files downloaded.")


def cmd_list(args):
    """List objects in the R2 bucket."""
    client = create_client(args)
    prefix = args.remote_path or ""

    keys = client.list_objects(prefix)
    if not keys:
        print("No objects found.")
        return

    print(f"Objects under prefix '{client._make_key(prefix)}':\n")
    for key in keys:
        print(f"  {key}")
    print(f"\n{len(keys)} objects total")


def main():
    parser = argparse.ArgumentParser(
        description="Upload/download TempusBench results to/from Cloudflare R2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Global R2 credential overrides
    parser.add_argument("--bucket", default="", help="R2 bucket name")
    parser.add_argument("--endpoint", default="", help="R2 endpoint URL")
    parser.add_argument("--access-key", default="", help="R2 access key ID")
    parser.add_argument("--secret-key", default="", help="R2 secret access key")
    parser.add_argument("--prefix", default="", help="R2 key prefix")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # upload subcommand
    upload_parser = subparsers.add_parser(
        "upload", help="Upload a local run directory or file to R2"
    )
    upload_parser.add_argument(
        "path", help="Local path to upload (file or directory, e.g. runs/run_20240101-120000)"
    )

    # download subcommand
    download_parser = subparsers.add_parser(
        "download", help="Download a run from R2 to local disk"
    )
    download_parser.add_argument(
        "remote_path", help="Remote prefix to download (e.g. run_20240101-120000)"
    )
    download_parser.add_argument(
        "local_dir",
        nargs="?",
        default=".",
        help="Local directory to save files (default: current directory)",
    )

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List objects in the R2 bucket")
    list_parser.add_argument(
        "remote_path",
        nargs="?",
        default="",
        help="Optional prefix to filter (e.g. run_20240101-120000)",
    )

    args = parser.parse_args()

    if args.command == "upload":
        cmd_upload(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
