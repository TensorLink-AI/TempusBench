#!/usr/bin/env python3
"""
Upload TempusBench benchmark results to Cloudflare R2.

Finds the evals/ directory from a run and uploads only the benchmark data
(evaluations.csv, pivot tables, aggregation CSVs) to R2 with a flat structure.

Usage:
    # Upload evals from the latest run
    python scripts/r2_sync.py upload

    # Upload evals from a specific run
    python scripts/r2_sync.py upload runs/run_20240101-120000

    # Upload a specific evaluations CSV
    python scripts/r2_sync.py upload runs/run_20240101-120000/evals/evaluations.csv

    # List what's in the bucket
    python scripts/r2_sync.py list

    # Download benchmark data from R2
    python scripts/r2_sync.py download ./output/

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


def find_latest_run() -> Path:
    """Find the most recent run directory under runs/."""
    runs_dir = project_root / "runs"
    if not runs_dir.is_dir():
        print("Error: No runs/ directory found")
        sys.exit(1)

    run_dirs = sorted(
        [d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith("run_")],
        reverse=True,
    )
    if not run_dirs:
        print("Error: No run directories found in runs/")
        sys.exit(1)

    return run_dirs[0]


def find_evals_csvs(evals_dir: Path) -> list:
    """Return all CSV files in an evals directory."""
    return sorted(evals_dir.glob("*.csv"))


def cmd_upload(args):
    """Upload benchmark evals data to R2."""
    client = create_client(args)

    if args.path:
        local_path = Path(args.path).resolve()
    else:
        # Default: latest run
        local_path = find_latest_run()
        print(f"Using latest run: {local_path.name}")

    if not local_path.exists():
        print(f"Error: {local_path} does not exist")
        sys.exit(1)

    # Figure out where the CSVs are
    if local_path.is_file() and local_path.suffix == ".csv":
        # Single CSV file
        csvs = [local_path]
    elif local_path.is_dir():
        evals_dir = local_path / "evals" if (local_path / "evals").is_dir() else local_path
        csvs = find_evals_csvs(evals_dir)
    else:
        print(f"Error: {local_path} is not a CSV file or directory")
        sys.exit(1)

    if not csvs:
        print("No CSV files found to upload")
        sys.exit(1)

    print(f"Uploading {len(csvs)} file(s) to R2 (prefix: {client.prefix}):\n")
    uploaded = 0
    for csv_path in csvs:
        r2_key = csv_path.name
        ok = client.upload_file(str(csv_path), r2_key)
        status = "ok" if ok else "FAILED"
        print(f"  {csv_path.name} -> {client._make_key(r2_key)}  [{status}]")
        if ok:
            uploaded += 1

    print(f"\n{uploaded}/{len(csvs)} files uploaded")


def cmd_download(args):
    """Download benchmark data from R2 to local disk."""
    client = create_client(args)
    local_dir = Path(args.local_dir).resolve()

    keys = client.list_objects("")
    if not keys:
        print("No objects found in R2")
        return

    # Filter to only CSV files
    csv_keys = [k for k in keys if k.endswith(".csv")]
    if not csv_keys:
        print("No CSV files found in R2")
        return

    local_dir.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(csv_keys)} file(s) to {local_dir}/:\n")

    downloaded = 0
    for key in csv_keys:
        # Strip prefix to get the filename
        filename = key.rsplit("/", 1)[-1]
        local_file = local_dir / filename
        try:
            local_file.parent.mkdir(parents=True, exist_ok=True)
            client._client.download_file(client.bucket, key, str(local_file))
            downloaded += 1
            print(f"  {key} -> {local_file}")
        except Exception as e:
            print(f"  FAILED: {key} ({e})")

    print(f"\n{downloaded}/{len(csv_keys)} files downloaded")


def cmd_list(args):
    """List benchmark data in the R2 bucket."""
    client = create_client(args)

    keys = client.list_objects("")
    if not keys:
        print("No objects found")
        return

    print(f"Benchmark data in s3://{client.bucket}/{client.prefix}/:\n")
    for key in keys:
        print(f"  {key}")
    print(f"\n{len(keys)} objects total")


def main():
    parser = argparse.ArgumentParser(
        description="Upload/download TempusBench benchmark results to/from R2",
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
        "upload", help="Upload benchmark evals to R2"
    )
    upload_parser.add_argument(
        "path",
        nargs="?",
        default="",
        help="Run directory or CSV file (default: latest run)",
    )

    # download
    download_parser = subparsers.add_parser(
        "download", help="Download benchmark data from R2"
    )
    download_parser.add_argument(
        "local_dir",
        nargs="?",
        default=".",
        help="Local directory to save files (default: current directory)",
    )

    # list
    subparsers.add_parser("list", help="List benchmark data in R2")

    args = parser.parse_args()

    if args.command == "upload":
        cmd_upload(args)
    elif args.command == "download":
        cmd_download(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
