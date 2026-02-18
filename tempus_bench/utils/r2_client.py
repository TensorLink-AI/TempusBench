"""
R2 (S3-compatible) storage client for streaming benchmark results to Cloudflare R2.

This module provides the R2StorageClient class which handles uploading benchmark
artifacts (CSVs, logs, TensorBoard events) to a Cloudflare R2 bucket using the
S3-compatible API via boto3.

When R2 is not configured or disabled, all operations are no-ops.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class R2StorageClient:
    """
    Client for uploading benchmark results to Cloudflare R2 (S3-compatible storage).

    Supports two usage patterns:
    - **Streaming**: Upload a file after each append (e.g., evaluations.csv after each
      model-task result). R2 doesn't support append, so this overwrites the object.
    - **Batch upload**: Upload an entire directory of artifacts at run completion.

    When disabled (r2_enabled=False or missing credentials), all methods are silent no-ops.

    Attributes:
        enabled (bool): Whether R2 uploads are active.
        bucket (str): R2 bucket name.
        prefix (str): Key prefix for all uploaded objects.
    """

    def __init__(
        self,
        enabled: bool = False,
        bucket: str = "",
        endpoint_url: str = "",
        access_key_id: str = "",
        secret_access_key: str = "",
        prefix: str = "tempusbench",
    ):
        """
        Initialize the R2 storage client.

        Credentials can be provided directly or via environment variables:
        - R2_ACCESS_KEY_ID / AWS_ACCESS_KEY_ID
        - R2_SECRET_ACCESS_KEY / AWS_SECRET_ACCESS_KEY
        - R2_ENDPOINT_URL
        - R2_BUCKET

        Args:
            enabled: Whether R2 uploading is enabled.
            bucket: R2 bucket name.
            endpoint_url: R2 S3-compatible endpoint URL.
            access_key_id: R2 access key ID (or set via env var).
            secret_access_key: R2 secret access key (or set via env var).
            prefix: Key prefix for all uploaded objects in the bucket.
        """
        self.enabled = enabled
        self.bucket = bucket or os.environ.get("R2_BUCKET", "")
        self.prefix = prefix
        self._client = None

        if not self.enabled:
            return

        # Resolve credentials from args or environment variables
        resolved_endpoint = endpoint_url or os.environ.get("R2_ENDPOINT_URL", "")
        resolved_key_id = (
            access_key_id
            or os.environ.get("R2_ACCESS_KEY_ID", "")
            or os.environ.get("AWS_ACCESS_KEY_ID", "")
        )
        resolved_secret = (
            secret_access_key
            or os.environ.get("R2_SECRET_ACCESS_KEY", "")
            or os.environ.get("AWS_SECRET_ACCESS_KEY", "")
        )

        if not all([self.bucket, resolved_endpoint, resolved_key_id, resolved_secret]):
            logger.warning(
                "R2 enabled but missing credentials/config. "
                "Provide bucket, endpoint_url, access_key_id, and secret_access_key "
                "via settings.yaml or environment variables. R2 uploads disabled."
            )
            self.enabled = False
            return

        try:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "s3",
                endpoint_url=resolved_endpoint,
                aws_access_key_id=resolved_key_id,
                aws_secret_access_key=resolved_secret,
                config=Config(
                    retries={"max_attempts": 3, "mode": "adaptive"},
                    connect_timeout=10,
                    read_timeout=30,
                ),
            )
            logger.info(
                f"R2 storage client initialized: bucket={self.bucket}, prefix={self.prefix}"
            )
        except ImportError:
            logger.warning(
                "boto3 not installed. Install with: pip install boto3. R2 uploads disabled."
            )
            self.enabled = False
        except Exception as e:
            logger.warning(f"Failed to initialize R2 client: {e}. R2 uploads disabled.")
            self.enabled = False

    def _make_key(self, relative_path: str) -> str:
        """
        Build the full R2 object key from the prefix and a relative path.

        Args:
            relative_path: Path relative to the run directory.

        Returns:
            Full R2 object key.
        """
        # Normalize separators to forward slashes
        relative_path = relative_path.replace("\\", "/")
        if self.prefix:
            return f"{self.prefix}/{relative_path}"
        return relative_path

    def upload_file(self, local_path: str, r2_key: str) -> bool:
        """
        Upload a single file to R2.

        Args:
            local_path: Absolute path to the local file.
            r2_key: Relative key within the prefix (e.g., "run_20240101/evals/evaluations.csv").

        Returns:
            True if upload succeeded, False otherwise.
        """
        if not self.enabled or self._client is None:
            return False

        full_key = self._make_key(r2_key)
        try:
            self._client.upload_file(local_path, self.bucket, full_key)
            logger.debug(f"Uploaded {local_path} -> s3://{self.bucket}/{full_key}")
            return True
        except Exception as e:
            logger.warning(f"Failed to upload {local_path} to R2: {e}")
            return False

    def upload_directory(self, local_dir: str, r2_key_prefix: str) -> int:
        """
        Upload all files in a local directory to R2, preserving directory structure.

        Args:
            local_dir: Absolute path to the local directory.
            r2_key_prefix: Prefix for all keys (e.g., "run_20240101").

        Returns:
            Number of files successfully uploaded.
        """
        if not self.enabled or self._client is None:
            return 0

        local_dir_path = Path(local_dir)
        if not local_dir_path.is_dir():
            logger.warning(f"Directory not found: {local_dir}")
            return 0

        uploaded = 0
        for file_path in local_dir_path.rglob("*"):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(local_dir_path)
            r2_key = f"{r2_key_prefix}/{relative}"
            if self.upload_file(str(file_path), r2_key):
                uploaded += 1

        logger.info(
            f"Uploaded {uploaded} files from {local_dir} to R2 prefix {r2_key_prefix}"
        )
        return uploaded

    def stream_file(self, local_path: str, r2_key: str) -> bool:
        """
        Stream (re-upload) a file to R2.

        This is used for files that are appended to incrementally (e.g., evaluations.csv).
        Since R2/S3 doesn't support append operations, we re-upload the entire file
        on each call. This gives consumers near-real-time access to results.

        Args:
            local_path: Absolute path to the local file.
            r2_key: Relative key within the prefix.

        Returns:
            True if upload succeeded, False otherwise.
        """
        return self.upload_file(local_path, r2_key)

    def download_file(self, r2_key: str, local_path: str) -> bool:
        """
        Download a file from R2 to local disk.

        Args:
            r2_key: Relative key within the prefix.
            local_path: Absolute path to save the downloaded file.

        Returns:
            True if download succeeded, False otherwise.
        """
        if not self.enabled or self._client is None:
            return False

        full_key = self._make_key(r2_key)
        try:
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            self._client.download_file(self.bucket, full_key, local_path)
            logger.debug(f"Downloaded s3://{self.bucket}/{full_key} -> {local_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to download {full_key} from R2: {e}")
            return False

    def list_objects(self, r2_key_prefix: str = "") -> list:
        """
        List objects in the bucket under a given prefix.

        Args:
            r2_key_prefix: Prefix to filter objects by (relative to self.prefix).

        Returns:
            List of object keys.
        """
        if not self.enabled or self._client is None:
            return []

        full_prefix = self._make_key(r2_key_prefix)
        try:
            keys = []
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=full_prefix):
                for obj in page.get("Contents", []):
                    keys.append(obj["Key"])
            return keys
        except Exception as e:
            logger.warning(f"Failed to list objects with prefix {full_prefix}: {e}")
            return []
