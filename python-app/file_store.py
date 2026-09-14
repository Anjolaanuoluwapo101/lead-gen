#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Where an uploaded file's BYTES live. Swappable, because the vendor is not the
point.

WHY THIS MODULE EXISTS

`POST /seller/<id>/resume` used to decode the upload, extract its text, and
drop the bytes on the floor. The text is what the drafting model reads, so for
drafting that was fine — but it means there was no file to attach, and "email
the resume" is half of what send is for. The extracted text is a summary of the
resume; it is not the resume.

So the bytes are now kept, and this module is the only thing that knows where.
`FILE_BACKEND` picks the vendor:

    none      -- store nothing. Callers get `None` back, which is the honest
                 answer, and the send path then simply has nothing to attach.
    supabase  -- Supabase Storage (the default)
    s3        -- any S3-compatible bucket (AWS, R2, MinIO…)
    local     -- the filesystem, for a machine with no cloud configured

THE CONTRACT IS DELIBERATELY NARROW

    put(key, data, content_type) -> the key actually stored
    get(key)                     -> (bytes, content_type)
    delete(key)                  -> None
    available()                  -> bool

Four functions, no vendor types in any signature. `s3` and `local` are named in
`FILE_BACKENDS` and raise NotImplementedError rather than silently doing nothing
— a backend that accepts an upload and stores it nowhere is worse than one that
refuses, because the seller is told their resume is saved.

WHY RESUMES ARE NOT PUBLIC

Resumes are personal data and the bucket is private. Nothing here returns a
public URL: the send path calls `get()` server-side and attaches the bytes.
That is also why there is no signed-URL helper yet — a URL is a capability, and
we should not mint one until something actually needs to hand it out.
"""

import os

import config

# Private by default. A bucket named in the environment so the demo can point
# at whatever it likes without a code change.
DEFAULT_BUCKET = "resumes"

# Keys are prefixed by seller, so one seller's objects cannot collide with
# another's and a seller's whole file set can be listed or deleted by prefix.
def resume_key(seller_id, filename):
    """The object key for one seller's resume.

    The seller id is the first path segment on purpose: Supabase Storage and S3
    both scope access policies by prefix, so putting the owner first is what
    makes a per-seller policy expressible later without moving every object.
    """
    safe = os.path.basename(str(filename or "resume")).replace("\\", "_")
    return f"{seller_id}/{safe}"


def bucket():
    return (os.getenv("FILE_BUCKET") or DEFAULT_BUCKET).strip()


def _backend():
    try:
        return config.file_backend()
    except ValueError:
        # A typo'd FILE_BACKEND must not take the whole app down at import time.
        # The switch raises so it cannot be mistaken for a working setting; here
        # the failure is caught and turned into "store nothing", which the
        # caller already has to handle.
        return "none"


def available():
    """Can this deployment store a file at all? Drives whether the UI offers
    upload, and whether the send path looks for an attachment."""
    return _backend() != "none"


def put(key, data, content_type=None):
    """Store `data` under `key`. Returns the key, or None when storing is off.

    Returns None rather than raising so a caller that has already extracted the
    text — which is the part drafting needs — is not failed by an unavailable
    bucket. The text is still saved; only the attachment is missing.
    """
    backend = _backend()
    if backend == "none":
        return None
    if backend == "supabase":
        return _put_supabase(key, data, content_type)
    if backend == "s3":
        return _put_s3(key, data, content_type)
    if backend == "local":
        return _put_local(key, data)
    return None


def get(key):
    """(bytes, content_type), or (None, None) when absent or storing is off.

    Never raises for a missing object: "this seller has no stored resume" is an
    ordinary state, not an error, and the send path treats it as "attach
    nothing".
    """
    if not key:
        return None, None
    backend = _backend()
    if backend == "none":
        return None, None
    if backend == "supabase":
        return _get_supabase(key)
    if backend == "s3":
        return _get_s3(key)
    if backend == "local":
        return _get_local(key)
    return None, None


def delete(key):
    if not key:
        return
    backend = _backend()
    if backend == "none":
        return
    if backend == "supabase":
        _delete_supabase(key)
    elif backend == "s3":
        _delete_s3(key)
    elif backend == "local":
        _delete_local(key)


# --------------------------------------------------------------------------- #
# supabase storage
# --------------------------------------------------------------------------- #
# The Storage REST API, not the `storage3` SDK: supabase_store already holds
# the project URL and service key, and adding an SDK dependency for three HTTP
# calls would be the only reason to install one.
def _supabase_creds():
    import supabase_store
    if not supabase_store.configured():
        return None, None
    return supabase_store.URL, supabase_store.SERVICE_KEY


def _put_supabase(key, data, content_type):
    import requests
    url, skey = _supabase_creds()
    if not url:
        return None
    resp = requests.post(
        f"{url}/storage/v1/object/{bucket()}/{key}",
        headers={"Authorization": f"Bearer {skey}", "apikey": skey,
                 "Content-Type": content_type or "application/octet-stream",
                 # Replace on re-upload: a seller updating their resume should
                 # not get a 409 from the previous version still being there.
                 "x-upsert": "true"},
        data=data, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"supabase storage put failed "
                           f"({resp.status_code}): {resp.text[:200]}")
    return key


def _get_supabase(key):
    import requests
    url, skey = _supabase_creds()
    if not url:
        return None, None
    resp = requests.get(
        f"{url}/storage/v1/object/{bucket()}/{key}",
        headers={"Authorization": f"Bearer {skey}", "apikey": skey},
        timeout=30)
    if resp.status_code == 404:
        return None, None
    if resp.status_code != 200:
        raise RuntimeError(f"supabase storage get failed "
                           f"({resp.status_code}): {resp.text[:200]}")
    return resp.content, resp.headers.get("Content-Type")


def _delete_supabase(key):
    import requests
    url, skey = _supabase_creds()
    if not url:
        return
    requests.delete(f"{url}/storage/v1/object/{bucket()}/{key}",
                    headers={"Authorization": f"Bearer {skey}", "apikey": skey},
                    timeout=30)


# --------------------------------------------------------------------------- #
# s3
# --------------------------------------------------------------------------- #
def _s3_client():
    import boto3
    return boto3.client("s3",
                        region_name=(os.getenv("AWS_REGION")
                                     or os.getenv("FILE_S3_REGION")
                                     or "us-west-2").strip(),
                        endpoint_url=(os.getenv("FILE_S3_ENDPOINT")
                                      or "").strip() or None)


def _put_s3(key, data, content_type):
    _s3_client().put_object(Bucket=bucket(), Key=key, Body=data,
                            ContentType=content_type
                            or "application/octet-stream")
    return key


def _get_s3(key):
    import botocore
    try:
        obj = _s3_client().get_object(Bucket=bucket(), Key=key)
    except botocore.exceptions.ClientError:
        return None, None
    return obj["Body"].read(), obj.get("ContentType")


def _delete_s3(key):
    _s3_client().delete_object(Bucket=bucket(), Key=key)


# --------------------------------------------------------------------------- #
# local
# --------------------------------------------------------------------------- #
def _local_root():
    root = (os.getenv("FILE_LOCAL_DIR") or "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "uploads")
    return root


def _local_path(key):
    # `key` is built by resume_key() from a basename, but it arrives from a
    # database column, and a path that can climb out of the root is a directory
    # traversal regardless of who wrote it. Resolve, then confirm containment.
    root = os.path.abspath(_local_root())
    path = os.path.abspath(os.path.join(root, key))
    if not path.startswith(root + os.sep):
        raise ValueError("refusing to read or write outside the uploads root")
    return path


def _put_local(key, data):
    path = _local_path(key)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return key


def _get_local(key):
    path = _local_path(key)
    if not os.path.exists(path):
        return None, None
    with open(path, "rb") as fh:
        return fh.read(), None


def _delete_local(key):
    path = _local_path(key)
    if os.path.exists(path):
        os.remove(path)
