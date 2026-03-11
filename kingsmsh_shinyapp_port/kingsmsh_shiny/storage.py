
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# NOTE: shinyapps.io does NOT provide reliable persistent local storage. For persistence you must
# use an external data store (e.g., S3, database, Drive). See Posit docs.
# This module provides:
#  - Local filesystem backend (good for local dev / Shiny Server / Posit Connect with persistent storage)
#  - Optional S3 backend (for shinyapps.io). Enable via env vars.

APP_DATA_DIR = Path(__file__).resolve().parent / "app_data"
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATE_DIR = APP_DATA_DIR / "templates"
ADJUST_DIR = APP_DATA_DIR / "adjustments"
TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
ADJUST_DIR.mkdir(parents=True, exist_ok=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in name)[:120]


class StorageError(RuntimeError):
    pass


@dataclass
class TemplateMeta:
    filename: str
    sha256: str


def backend_name() -> str:
    return os.environ.get("STORAGE_BACKEND", "local").lower().strip()


# -------------------------
# Local backend
# -------------------------
def local_save_template(src_path: str) -> TemplateMeta:
    src = Path(src_path)
    if not src.exists():
        raise StorageError("Template upload missing on disk.")
    fname = _safe_name(src.name)
    dst = TEMPLATE_DIR / fname
    shutil.copy2(src, dst)
    return TemplateMeta(filename=fname, sha256=_sha256_file(dst))


def local_get_template_path() -> Optional[Path]:
    # Single "active" template file for minimal implementation
    marker = TEMPLATE_DIR / "_active.txt"
    if not marker.exists():
        return None
    fname = marker.read_text(encoding="utf-8").strip()
    p = TEMPLATE_DIR / fname
    return p if p.exists() else None


def local_set_active_template(filename: str) -> None:
    (TEMPLATE_DIR / "_active.txt").write_text(filename, encoding="utf-8")


def local_load_adjustment() -> Dict[str, Any]:
    p = ADJUST_DIR / "adjustment.json"
    if not p.exists():
        return {"modules": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"modules": {}}


def local_save_adjustment(adjustment: Dict[str, Any]) -> None:
    p = ADJUST_DIR / "adjustment.json"
    p.write_text(json.dumps(adjustment, ensure_ascii=False, indent=2), encoding="utf-8")


# -------------------------
# S3 backend (optional)
# -------------------------
def _s3_client():
    try:
        import boto3  # type: ignore
    except Exception as e:
        raise StorageError("boto3 is required for S3 backend. Add it to requirements.txt.") from e

    kwargs = {}
    endpoint = os.environ.get("S3_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def s3_bucket() -> str:
    b = os.environ.get("S3_BUCKET", "").strip()
    if not b:
        raise StorageError("S3_BUCKET is not set.")
    return b


def s3_prefix() -> str:
    return os.environ.get("S3_PREFIX", "kingsmsh").strip().rstrip("/")


def s3_save_template(src_path: str) -> TemplateMeta:
    cli = _s3_client()
    bucket = s3_bucket()
    src = Path(src_path)
    fname = _safe_name(src.name)
    key = f"{s3_prefix()}/templates/{fname}"
    with src.open("rb") as f:
        cli.upload_fileobj(f, bucket, key)
    sha = _sha256_file(src)
    # marker
    cli.put_object(Bucket=bucket, Key=f"{s3_prefix()}/templates/_active.txt", Body=fname.encode("utf-8"))
    return TemplateMeta(filename=fname, sha256=sha)


def s3_download_active_template(dst_dir: Path) -> Optional[Path]:
    cli = _s3_client()
    bucket = s3_bucket()
    try:
        obj = cli.get_object(Bucket=bucket, Key=f"{s3_prefix()}/templates/_active.txt")
        fname = obj["Body"].read().decode("utf-8").strip()
    except Exception:
        return None
    if not fname:
        return None
    key = f"{s3_prefix()}/templates/{fname}"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / fname
    try:
        with dst.open("wb") as f:
            cli.download_fileobj(bucket, key, f)
        return dst
    except Exception:
        return None


def s3_load_adjustment() -> Dict[str, Any]:
    cli = _s3_client()
    bucket = s3_bucket()
    key = f"{s3_prefix()}/adjustments/adjustment.json"
    try:
        obj = cli.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return {"modules": {}}


def s3_save_adjustment(adjustment: Dict[str, Any]) -> None:
    cli = _s3_client()
    bucket = s3_bucket()
    key = f"{s3_prefix()}/adjustments/adjustment.json"
    cli.put_object(Bucket=bucket, Key=key, Body=json.dumps(adjustment, ensure_ascii=False, indent=2).encode("utf-8"))


# -------------------------
# Unified API
# -------------------------
def save_template(uploaded_path: str) -> TemplateMeta:
    if backend_name() == "s3":
        return s3_save_template(uploaded_path)
    meta = local_save_template(uploaded_path)
    local_set_active_template(meta.filename)
    return meta


def get_active_template_local_path(tmp_dir: Path) -> Optional[Path]:
    """
    Returns a local filesystem path to the active template (downloaded from S3 if needed).
    """
    if backend_name() == "s3":
        return s3_download_active_template(tmp_dir)
    return local_get_template_path()


def load_adjustment() -> Dict[str, Any]:
    if backend_name() == "s3":
        return s3_load_adjustment()
    return local_load_adjustment()


def save_adjustment(adjustment: Dict[str, Any]) -> None:
    if backend_name() == "s3":
        s3_save_adjustment(adjustment)
    else:
        local_save_adjustment(adjustment)
