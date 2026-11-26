"""Google JSON API transport with restartable, checksum-checked transfers."""
import base64
import hashlib
import re
from urllib.parse import quote, urlparse
import requests

class CloudHTTPError(RuntimeError):
    def __init__(self, action, status):
        self.status = status
        super().__init__(f"{action}: HTTP {status}. Check Cloud permissions, billing and connectivity; retained files are safe.")


class CloudServiceError(RuntimeError):
    """Actionable service diagnostics without bearer URLs or credential values."""


def safe_service_message(error):
    text = re.sub(r"https?://[^\s]+", "[URL omitted]", str(error))
    sensitive = re.search(r"(?i)authorization|access_token|refresh_token|password|upload_id", text)
    if sensitive:
        text = text[:sensitive.start()] + "[credential details omitted]"
    return text[:700]


def file_checksums(path):
    sha = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha.update(chunk); md5.update(chunk)
    return sha.hexdigest(), base64.b64encode(md5.digest()).decode()


class GoogleTransport:
    def __init__(self, config, credentials=None, http=None, ee_module=None):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        import ee
        self.config = config
        if credentials is None:
            credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform",
                                                         "https://www.googleapis.com/auth/earthengine"])
        self.http = http or AuthorizedSession(credentials)
        self.ee = ee_module or ee
        self.ee.Initialize(credentials=credentials, project=config.project)

    def request(self, method, url, *, action, **kwargs):
        try:
            return self.http.request(method, url, timeout=(30, 180), allow_redirects=False, **kwargs)
        except requests.RequestException:
            # Requests exceptions include bearer session URLs; never propagate/log them.
            raise RuntimeError(f"{action}: network interruption; resume the saved queue.") from None

    def bucket_info(self):
        r = self.request("GET", f"https://storage.googleapis.com/storage/v1/b/{quote(self.config.bucket, safe='')}", action="Read bucket")
        if r.status_code != 200:
            raise CloudHTTPError("Read bucket", r.status_code)
        result = r.json()
        if result.get("storageClass") != "STANDARD":
            raise ValueError("Use a dedicated Standard-class staging bucket (other classes can charge minimum retention/retrieval).")
        if result.get("retentionPolicy", {}).get("retentionPeriod"):
            raise ValueError("The staging bucket has enforced retention; use a dedicated temporary bucket without retention.")
        required = ["storage.objects.create", "storage.objects.get"]
        if self.config.delete_after_verification:
            required.append("storage.objects.delete")
        permissions = self.request("GET", f"https://storage.googleapis.com/storage/v1/b/{quote(self.config.bucket, safe='')}/iam/testPermissions",
            action="Check bucket permissions", params=[("permissions", name) for name in required])
        if permissions.status_code != 200:
            raise CloudHTTPError("Check bucket permissions", permissions.status_code)
        missing = set(required) - set(permissions.json().get("permissions", []))
        if missing:
            raise ValueError("Cloud Upload lacks bucket permissions: " + ", ".join(sorted(missing)))
        if result.get("billing", {}).get("requesterPays"):
            raise ValueError("Use a dedicated staging bucket without Requester Pays.")
        return result

    def object_url(self, row):
        return f"https://storage.googleapis.com/storage/v1/b/{quote(row['bucket'], safe='')}/o/{quote(row['object'], safe='')}"

    def object(self, row):
        r = self.request("GET", self.object_url(row), action="Read staged object")
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise CloudHTTPError("Read staged object", r.status_code)
        return r.json()

    @staticmethod
    def check_object(row, obj):
        if not (obj.get("metadata", {}).get("swotflow_owner") == row["owner"]
                and obj.get("metadata", {}).get("sha256") == row["sha256"]
                and obj.get("md5Hash") == row["md5"] and int(obj.get("size", -1)) == row["size"]
                and obj.get("generation")):
            raise ValueError("Staging object ownership, length or checksum mismatch; nothing was ingested or deleted.")
        if row.get("generation") and str(row["generation"]) != str(obj["generation"]):
            raise ValueError("Staging object generation changed; manual review required.")
        return str(obj["generation"])

    def upload(self, row, path, secrets, progress=lambda offset: None):
        obj = self.object(row)
        if obj:
            return self.check_object(row, obj)
        key = row["secret_id"]
        session = secrets.load(key)
        offset = 0
        if session:
            parsed = urlparse(session)
            if parsed.scheme != "https" or parsed.hostname != "storage.googleapis.com":
                raise ValueError("Invalid stored upload endpoint.")
            r = self.request("PUT", session, action="Resume transfer", data=b"",
                             headers={"Content-Length": "0", "Content-Range": f"bytes */{row['size']}"})
            if r.status_code in {404, 410}:
                secrets.remove(key); session = ""
            elif r.status_code in {200, 201}:
                generation = self.check_object(row, r.json()); secrets.remove(key)
                return generation
            elif r.status_code == 308:
                offset = int(r.headers["Range"].rsplit("-", 1)[1]) + 1 if r.headers.get("Range") else 0
            else:
                raise CloudHTTPError("Resume transfer", r.status_code)
        if not session:
            r = self.request("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{quote(row['bucket'], safe='')}/o",
                action="Create resumable transfer", params={"uploadType": "resumable", "name": row["object"], "ifGenerationMatch": "0"},
                json={"contentType": "image/tiff", "md5Hash": row["md5"],
                      "metadata": {"swotflow_owner": row["owner"], "sha256": row["sha256"]}},
                headers={"X-Upload-Content-Type": "image/tiff", "X-Upload-Content-Length": str(row["size"])})
            if r.status_code not in {200, 201}:
                raise CloudHTTPError("Create resumable transfer", r.status_code)
            session = r.headers.get("Location", "")
            if urlparse(session).scheme != "https" or urlparse(session).hostname != "storage.googleapis.com":
                raise ValueError("Unexpected resumable upload endpoint.")
            secrets.save(key, session)
        if not 0 <= offset < row["size"]:
            raise ValueError("Invalid resumable offset; retained for review.")
        with path.open("rb") as stream:
            stream.seek(offset)
            while offset < row["size"]:
                chunk = stream.read(min(self.config.chunk_mib * 1024**2, row["size"] - offset))
                if not chunk:
                    raise ValueError("Local file was truncated during upload.")
                end = offset + len(chunk) - 1
                r = self.request("PUT", session, action="Transfer mosaic", data=chunk,
                    headers={"Content-Type": "image/tiff", "Content-Range": f"bytes {offset}-{end}/{row['size']}"})
                if r.status_code in {200, 201}:
                    generation = self.check_object(row, r.json())
                    progress(row["size"]); secrets.remove(key)
                    return generation
                if r.status_code != 308:
                    raise CloudHTTPError("Transfer mosaic", r.status_code)
                acknowledged = int(r.headers["Range"].rsplit("-", 1)[1]) + 1 if r.headers.get("Range") else 0
                if not offset < acknowledged <= end + 1:
                    raise ValueError("Unexpected server upload offset; resume will reconcile it.")
                offset = acknowledged; stream.seek(offset); progress(offset)
        raise RuntimeError("Transfer finished without a verified final object response; resume will reconcile it.")

    def delete(self, row):
        obj = self.object(row)
        if obj is None:
            return  # Already deleted after a crash, or externally absent: record as absent.
        self.check_object(row, obj)
        r = self.request("DELETE", self.object_url(row), action="Delete verified staging object",
                         params={"ifGenerationMatch": row["generation"]})
        if r.status_code not in {204, 404}:
            raise CloudHTTPError("Delete verified staging object", r.status_code)

    def asset(self, asset_id):
        try:
            return self.ee.data.getAsset(asset_id)
        except Exception as exc:
            # Only an explicit not-found response permits a submission decision.
            if "not found" in str(exc).lower() or "does not exist" in str(exc).lower():
                return None
            raise

    def submit(self, request_id, manifest):
        try:
            result = self.ee.data.startIngestion(request_id, manifest, allow_overwrite=False)
            return result.get("name") or result.get("id")
        except Exception as exc:
            raise CloudServiceError("EE ingestion request: " + safe_service_message(exc)) from None

    def operation(self, name):
        return self.ee.data.getOperation(name)
