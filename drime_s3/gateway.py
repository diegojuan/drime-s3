"""S3 Gateway WSGI Application for Drime Cloud."""

import hashlib
import io
import json
import logging
import os
import shutil
import base64
import tempfile
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree.ElementTree import Element, tostring, fromstring
from urllib.parse import parse_qs

import httpx

from .api import DrimeClient
from .models import FileEntry

logger = logging.getLogger(__name__)

def format_http_date(iso_date: str) -> str:
    """Convert ISO 8601 date to HTTP date format (RFC 2822)."""
    from email.utils import formatdate
    from datetime import datetime
    
    if not iso_date:
        return formatdate(usegmt=True)
    
    try:
        iso_date = iso_date.replace('Z', '+00:00')
        if '.' in iso_date:
            dt = datetime.fromisoformat(iso_date.split('.')[0] + '+00:00')
        else:
            dt = datetime.fromisoformat(iso_date)
        return formatdate(dt.timestamp(), usegmt=True)
    except:
        return formatdate(usegmt=True)


def format_iso_date(iso_date: str) -> str:
    """Ensure ISO 8601 date format for XML responses."""
    if not iso_date:
        return "2023-01-01T00:00:00Z"
    if not iso_date.endswith('Z') and '+' not in iso_date:
        iso_date = iso_date + 'Z'
    return iso_date.replace('.000000', '')


def create_xml_response(root_tag: str, data: Dict[str, Any]) -> bytes:
    """Create S3 XML response."""
    root = Element(root_tag, xmlns="http://s3.amazonaws.com/doc/2006-03-01/")
    
    def add_children(parent, child_data):
        for key, value in child_data.items():
            if isinstance(value, list):
                for item in value:
                    child = Element(key)
                    parent.append(child)
                    if isinstance(item, dict):
                        add_children(child, item)
                    else:
                        child.text = str(item)
            elif isinstance(value, dict):
                child = Element(key)
                parent.append(child)
                add_children(child, value)
            else:
                child = Element(key)
                if value is not None:
                    child.text = str(value)
                parent.append(child)
    
    add_children(root, data)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding='utf-8')


class S3Gateway:
    """WSGI Application that translates S3 requests to Drime API calls."""
    
    def __init__(self, client: DrimeClient):
        self.client = client
        
        # Local MD5 metadata store
        self.metadata_file = Path(os.path.expanduser("~/.config/drime-s3/metadata.json"))
        self.metadata: Dict[str, str] = {}
        self._load_metadata()
        
        # Folder cache
        self._folder_cache: Dict[str, int] = {}
        
        # Multipart Size Cache (UploadID -> Size) - from Init headers
        self._upload_size_cache: Dict[str, int] = {}
        
        # Multipart Parts Size Tracker (UploadID -> {PartNum: Size}) - accumulated during upload
        self._upload_parts_sizes: Dict[str, Dict[int, int]] = {}
    
    def _load_metadata(self):
        try:
            if self.metadata_file.exists():
                with open(self.metadata_file) as f:
                    self.metadata = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load metadata: {e}")
    
    def _save_metadata(self):
        try:
            self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.metadata_file, "w") as f:
                json.dump(self.metadata, f)
        except Exception as e:
            logger.warning(f"Failed to save metadata: {e}")
    
    def _get_etag(self, entry: FileEntry) -> str:
        """Get ETag from local metadata or fallback."""
        eid = str(entry.id)
        if eid in self.metadata:
            return f'"{self.metadata[eid]}"'
        if entry.description and entry.description.startswith("md5:"):
            return f'"{entry.description[4:]}"'
        return f'"{entry.hash}"' if entry.hash else '"unknown"'

    def _encode_multipart_id(self, upload_id: str, key: str) -> str:
        """Encode internal uploadId and key into a single S3 UploadID."""
        data = json.dumps({"uid": upload_id, "key": key}).encode("utf-8")
        return base64.urlsafe_b64encode(data).decode("utf-8")

    def _decode_multipart_id(self, composite_id: str) -> Tuple[str, str]:
        """Decode S3 UploadID back to internal uploadId and key."""
        try:
            composite_id = composite_id.strip()
            missing_padding = len(composite_id) % 4
            if missing_padding:
                composite_id += '=' * (4 - missing_padding)
            
            data = base64.urlsafe_b64decode(composite_id)
            decoded = json.loads(data)
            return decoded["uid"], decoded["key"]
        except Exception as e:
            logger.error(f"Failed to decode UploadId {composite_id}: {e}")
            raise ValueError("Invalid UploadId")
    
    def _resolve_key(self, key: str) -> Tuple[Optional[FileEntry], Optional[int], str]:
        """Resolve S3 key to Drime FileEntry."""
        key = key.strip("/")
        if not key:
            return None, None, ""
        
        parts = key.split("/")
        entry_name = parts[-1]
        parent_path = "/".join(parts[:-1])
        
        parent_id = None
        if parent_path:
            # Walk the path
            parent_parts = parent_path.split("/")
            current_pid = None
            
            for folder_name in parent_parts:
                entries = self.client.list_folder(current_pid)
                found = None
                for e in entries:
                    if e.name.lower() == folder_name.lower() and e.is_folder:
                        found = e
                        break
                if found:
                    current_pid = found.id
                else:
                    return None, None, entry_name
            parent_id = current_pid
        
        # Find entry in parent
        entries = self.client.list_folder(parent_id)
        for e in entries:
            if e.name == entry_name:
                return e, parent_id, entry_name
        
        return None, parent_id, entry_name
    
    def _ensure_folder(self, path: str) -> Optional[int]:
        """Ensure folder path exists, creating if needed."""
        if not path:
            return None
        
        parts = path.strip("/").split("/")
        current_pid = None
        
        for folder_name in parts:
            entries = self.client.list_folder(current_pid)
            found = None
            for e in entries:
                if e.name.lower() == folder_name.lower() and e.is_folder:
                    found = e
                    break
            
            if found:
                current_pid = found.id
            else:
                try:
                    result = self.client.create_folder(folder_name, current_pid)
                    folder_data = result.get("folder", {})
                    current_pid = folder_data.get("id")
                except Exception as e:
                    if "422" in str(e):
                        # Folder exists but not found - race condition, retry list multiple times
                        found_after_retry = False
                        for retry in range(3):
                            import time
                            time.sleep(0.1 * (retry + 1))  # Small delay before retry
                            entries = self.client.list_folder(current_pid)
                            for e2 in entries:
                                if e2.name.lower() == folder_name.lower() and e2.is_folder:
                                    current_pid = e2.id
                                    found_after_retry = True
                                    break
                            if found_after_retry:
                                break
                        if not found_after_retry:
                            logger.warning(f"Could not find folder '{folder_name}' after 422 error, parent_id={current_pid}")
                            # Don't update current_pid - this will cause upload to wrong folder
                            raise Exception(f"Race condition: folder '{folder_name}' not found after 422")
                    else:
                        raise
        
        return current_pid
    
    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        query_string = environ.get("QUERY_STRING", "")
        params = parse_qs(query_string, keep_blank_values=True)
        
        logger.info(f"S3 Request: {method} {path} ?{query_string}")
        
        try:
            if path == "/":
                return self.handle_service(environ, start_response)
            
            # Parse bucket/key from path
            path = path.lstrip("/")
            if "/" in path:
                bucket, key = path.split("/", 1)
            else:
                bucket, key = path, ""
            
            # Multipart Indicators
            is_multipart_init = "uploads" in params
            upload_id = params.get("uploadId", [None])[0]
            part_number = params.get("partNumber", [None])[0]

            if method == "GET":
                if not key:
                    return self.handle_list_bucket(environ, start_response, bucket)
                return self.handle_get_object(environ, start_response, bucket, key)
            elif method == "HEAD":
                if not key:
                    # HEAD bucket
                    if bucket == "default":
                        start_response("200 OK", [("Content-Length", "0")])
                        return []
                    return self.send_error(start_response, "404 Not Found", "NoSuchBucket", "Bucket not found")
                return self.handle_head_object(environ, start_response, bucket, key)
            elif method == "PUT":
                if not key:
                    # PUT bucket
                    start_response("200 OK", [("Content-Length", "0")])
                    return []
                if part_number and upload_id:
                     return self.handle_upload_part(environ, start_response, bucket, key, upload_id, part_number)
                return self.handle_put_object(environ, start_response, bucket, key)
            elif method == "POST" and key:
                if is_multipart_init:
                    return self.handle_create_multipart_upload(environ, start_response, bucket, key)
                elif upload_id:
                    return self.handle_complete_multipart_upload(environ, start_response, bucket, key, upload_id)
                else:
                    return self.send_error(start_response, "404 Not Found", "InvalidURI", "Unsupported POST request")
            elif method == "DELETE" and key:
                if upload_id:
                    return self.handle_abort_multipart_upload(environ, start_response, bucket, key, upload_id)
                return self.handle_delete_object(environ, start_response, bucket, key)

            
            return self.send_error(start_response, "400 Bad Request", "InvalidURI", "Invalid URI")
        
        except Exception as e:
            logger.exception("S3 Gateway Error")
            return self.send_error(start_response, "500 Internal Server Error", "InternalError", str(e))
    
    def send_response(self, start_response, status, content, content_type="application/xml", headers=None):
        resp_headers = [
            ("Content-Type", content_type),
            ("Content-Length", str(len(content))),
            ("Date", formatdate(usegmt=True)),
            ("Server", "DrimeS3"),
        ]
        if headers:
            resp_headers.extend(headers)
        start_response(status, resp_headers)
        return [content]
    
    def send_error(self, start_response, status, code, message, include_body=True):
        """Send standard S3 XML Error."""
        xml = create_xml_response("Error", {"Code": code, "Message": message})
        
        if not include_body:
             headers = [
                ("Content-Type", "application/xml"),
                ("Content-Length", str(len(xml))),
                ("Date", formatdate(usegmt=True)),
                ("Server", "DrimeS3"),
             ]
             start_response(status, headers)
             return []
             
        return self.send_response(start_response, status, xml)

    # ------------------ Multipart Handlers ------------------

    def handle_create_multipart_upload(self, environ, start_response, bucket, key):
        """Initiate Multipart Upload."""
        try:
            extension = os.path.splitext(key)[1].lstrip(".")
            filename = os.path.basename(key)
            dirname = os.path.dirname(key)
            
            # Resolve parent
            parent_id = 0
            if dirname:
                try:
                    parent_id = self._ensure_folder(dirname)
                except:
                     parent_id = 0
            
            # Attempt to get size from header if provided
            size = 0
            # Try custom headers first (Rclone can send metadata)
            for header in ["HTTP_X_AMZ_META_SIZE", "HTTP_X_FILE_SIZE", "HTTP_CONTENT_LENGTH_RANGE"]:
                 val = environ.get(header)
                 if val:
                     try:
                         size = int(val)
                         break
                     except:
                         pass
                
            init_data = {
                "filename": filename,
                "mime": "application/octet-stream",
                "size": size, # Placeholder or 0
                "extension": extension,
                "relativePath": filename,
                "workspaceId": 0,
            }
            if parent_id:
                 init_data["parentId"] = parent_id

            resp = self.client._request("POST", "/s3/multipart/create", json=init_data)
            init_resp = resp.json()
             
            drime_upload_id = init_resp.get("uploadId")
            drime_key = init_resp.get("key")
             
            if not drime_upload_id or not drime_key:
                 raise Exception("Invalid multipart init response")
            
            # Cache the known size for completion
            if size > 0:
                self._upload_size_cache[drime_upload_id] = size
             
            # Create composite ID
            composite_id = self._encode_multipart_id(drime_upload_id, drime_key)
             
            data = {
                 "Bucket": bucket,
                 "Key": key,
                 "UploadId": composite_id
            }
            return self.send_response(start_response, "200 OK", create_xml_response("InitiateMultipartUploadResult", data))

        except Exception as e:
            logger.error(f"Multipart Init Failed: {e}")
            return self.send_error(start_response, "500 Internal Error", "InternalError", str(e))

    def handle_upload_part(self, environ, start_response, bucket, key, upload_id, part_number):
        """Streaming Proxy for Upload Part."""
        try:
            drime_uid, drime_key = self._decode_multipart_id(upload_id)
            part_num = int(part_number)
            
            # Get Signed URL
            sign_req = {
                "key": drime_key,
                "uploadId": drime_uid,
                "partNumbers": [part_num]
            }
            resp = self.client._request("POST", "/s3/multipart/batch-sign-part-urls", json=sign_req)
            sign_data = resp.json()
            
            urls = sign_data.get("urls", [])
            if not urls:
                 raise Exception("No signed URL returned")
            signed_url = urls[0]["url"]
            
            # Stream body to S3 with size tracking
            content_length = environ.get("CONTENT_LENGTH")
            cl_int = int(content_length) if content_length else 0
            
            # Track part size locally to calculate total later
            if drime_uid not in self._upload_parts_sizes:
                self._upload_parts_sizes[drime_uid] = {}
            self._upload_parts_sizes[drime_uid][part_num] = cl_int
            
            stream = environ["wsgi.input"]
            def input_reader():
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    yield chunk
            
            headers = {"Content-Type": "application/octet-stream"}
            if cl_int:
                 headers["Content-Length"] = str(cl_int)
            
            with httpx.Client() as client:
                s3_resp = client.put(signed_url, content=input_reader(), headers=headers, timeout=None)
                s3_resp.raise_for_status()
                
                etag = s3_resp.headers.get("ETag", "").strip('"')
                if not etag:
                     etag = "no-etag"
                
                return self.send_response(start_response, "200 OK", b"", headers=[("ETag", f'"{etag}"')])

        except Exception as e:
            logger.error(f"Upload Part Failed: {e}")
            return self.send_error(start_response, "500 Internal Error", "InternalError", str(e))

    def handle_complete_multipart_upload(self, environ, start_response, bucket, key, upload_id):
        """Complete Multipart Upload."""
        try:
            drime_uid, drime_key = self._decode_multipart_id(upload_id)
            
            # Parse XML for parts
            length = int(environ.get("CONTENT_LENGTH", 0))
            body = environ["wsgi.input"].read(length)
            root = fromstring(body)
            
            # Handle XML namespace optionality
            parts = []
            ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
            
            # Try namespaced first
            items = root.findall(f"{ns}Part")
            if not items:
                items = root.findall("Part")
                
            for p in items:
                pn = p.find(f"{ns}PartNumber") if p.find(f"{ns}PartNumber") is not None else p.find("PartNumber")
                et = p.find(f"{ns}ETag") if p.find(f"{ns}ETag") is not None else p.find("ETag")
                if pn is not None and et is not None:
                     parts.append({
                         "PartNumber": int(pn.text),
                         "ETag": et.text.strip('"')
                     })
            
            # Call complete
            comp_resp = self.client._request("POST", "/s3/multipart/complete", json={
                "key": drime_key,
                "uploadId": drime_uid,
                "parts": parts
            })
            logger.info(f"Multipart Complete Response: {comp_resp.json()}")
            
            # Create File Entry
            filename = os.path.basename(key)
            extension = os.path.splitext(key)[1].lstrip(".")
            dirname = os.path.dirname(key)
            parent_id = self._ensure_folder(dirname) if dirname else None
            
            # Recuperar tamaño del cache (Init header) o calcular sumando partes
            final_size = self._upload_size_cache.pop(drime_uid, 0)
            
            if final_size == 0:
                # Try to calculate from tracked parts
                parts_sizes = self._upload_parts_sizes.pop(drime_uid, {})
                for p in parts: # parts from XML
                    pnum = p.get("PartNumber")
                    if pnum in parts_sizes:
                        final_size += parts_sizes[pnum]
                
                if final_size == 0 and parts_sizes:
                     # Fallback simple sum if missing XML match
                     final_size = sum(parts_sizes.values())
            else:
                 # Cleanup parts cache anyway
                 self._upload_parts_sizes.pop(drime_uid, None)
            
            entry_data = {
                "clientMime": "application/octet-stream",
                "clientName": filename,
                "filename": drime_key.split("/")[-1],
                "clientExtension": extension,
                "relativePath": filename,
                "workspaceId": 0,
                "size": final_size,
            }
            if parent_id is not None:
                entry_data["parentId"] = parent_id
            
            self.client._request("POST", "/s3/entries", json=entry_data)
            
            res_data = {
                "Location": f"http://localhost:8080/{bucket}/{key}",
                "Bucket": bucket,
                "Key": key,
                "ETag": '"complete"'
            }
            return self.send_response(start_response, "200 OK", create_xml_response("CompleteMultipartUploadResult", res_data))

        except Exception as e:
            logger.error(f"Complete Failed: {e}")
            return self.send_error(start_response, "500 Internal Error", "InternalError", str(e))

    def handle_abort_multipart_upload(self, environ, start_response, bucket, key, upload_id):
        try:
            drime_uid, drime_key = self._decode_multipart_id(upload_id)
            self.client._request("POST", "/s3/multipart/abort", json={"key": drime_key, "uploadId": drime_uid})
            # Cleanup cache
            self._upload_size_cache.pop(drime_uid, None)
            self._upload_parts_sizes.pop(drime_uid, None)
            return self.send_response(start_response, "204 No Content", b"")
        except Exception as e:
            return self.send_error(start_response, "500 Internal Error", "InternalError", str(e))
    
    def handle_service(self, environ, start_response):
        """List buckets."""
        data = {
            "Owner": {"ID": "drime", "DisplayName": "drime"},
            "Buckets": {"Bucket": [{"Name": "default", "CreationDate": "2023-01-01T00:00:00.000Z"}]}
        }
        return self.send_response(start_response, "200 OK", create_xml_response("ListAllMyBucketsResult", data))
    
    def _list_recursive(self, folder_id: Optional[int], prefix: str) -> list:
        """Recursively list all files in folder and subfolders."""
        results = []
        entries = self.client.list_folder(folder_id)
        
        for entry in entries:
            full_key = prefix + entry.name if prefix else entry.name
            
            if entry.is_folder:
                # Recurse into subfolder
                sub_results = self._list_recursive(entry.id, full_key + "/")
                results.extend(sub_results)
            else:
                results.append({
                    "Key": full_key,
                    "LastModified": format_iso_date(entry.updated_at),
                    "ETag": self._get_etag(entry),
                    "Size": entry.file_size,
                    "StorageClass": "STANDARD",
                })
        
        return results

    def handle_list_bucket(self, environ, start_response, bucket):
        """List objects in bucket."""
        if bucket != "default":
            return self.send_error(start_response, "404 Not Found", "NoSuchBucket", "Only 'default' bucket")
        
        from urllib.parse import parse_qs
        params = parse_qs(environ.get("QUERY_STRING", ""))
        prefix = params.get("prefix", [""])[0]
        delimiter = params.get("delimiter", [""])[0]
        
        # Resolve folder from prefix
        folder_id = None
        base_prefix = ""
        if prefix:
            folder_path = prefix.rstrip("/")
            if folder_path:
                folder_id = self._ensure_folder(folder_path)
                base_prefix = folder_path + "/"
        
        contents = []
        prefixes = set()
        
        if delimiter:
            # Non-recursive listing (with delimiter)
            entries = self.client.list_folder(folder_id)
            for entry in entries:
                full_key = base_prefix + entry.name if base_prefix else entry.name
                
                if entry.is_folder:
                    prefixes.add(full_key + "/")
                else:
                    contents.append({
                        "Key": full_key,
                        "LastModified": format_iso_date(entry.updated_at),
                        "ETag": self._get_etag(entry),
                        "Size": entry.file_size,
                        "StorageClass": "STANDARD",
                    })
        else:
            # Recursive listing (no delimiter) - required for restic
            contents = self._list_recursive(folder_id, base_prefix)
        
        response_data = {
            "Name": bucket,
            "Prefix": prefix,
            "KeyCount": len(contents) + len(prefixes),
            "MaxKeys": 10000,
            "IsTruncated": "false",
            "Contents": contents,
        }
        if prefixes:
            response_data["CommonPrefixes"] = [{"Prefix": p} for p in sorted(prefixes)]
        
        return self.send_response(start_response, "200 OK", create_xml_response("ListBucketResult", response_data))
    
    def handle_get_object(self, environ, start_response, bucket, key):
        """Download object."""
        entry, _, _ = self._resolve_key(key)
        if not entry:
            return self.send_error(start_response, "404 Not Found", "NoSuchKey", "Key not found")
        if entry.is_folder:
            return self.send_error(start_response, "400 Bad Request", "InvalidRequest", "Cannot download folder")
        
        # Use entry.url if available, otherwise construct download URL
        download_url = None
        if entry.url:
            if entry.url.startswith("http"):
                download_url = entry.url
            else:
                # Relative URL like "api/v1/file-entries/ID/..."
                base = self.client.api_url.replace("/api/v1", "")
                download_url = f"{base}/{entry.url}"
        
        if not download_url:
            download_url = self.client.get_download_url(entry.id)
        
        headers = {"Authorization": f"Bearer {self.client.api_key}"}
        
        # Download to buffer to get accurate Content-Length
        try:
            response = httpx.get(download_url, headers=headers, follow_redirects=True, timeout=300)
            if response.status_code != 200:
                return self.send_error(start_response, "500 Internal Error", "DownloadFailed", f"Status {response.status_code}")
            
            data = response.content
            
            import hashlib
            get_md5 = hashlib.md5(data).hexdigest()
            
            resp_headers = [
                ("Content-Type", entry.mime or "application/octet-stream"),
                ("Content-Length", str(len(data))),
                ("Last-Modified", format_http_date(entry.updated_at)),
                ("ETag", self._get_etag(entry)),
            ]
            start_response("200 OK", resp_headers)
            return [data]
        except Exception as e:
            logger.exception("Download error")
            return self.send_error(start_response, "500 Internal Error", "DownloadFailed", str(e))

    def handle_put_object(self, environ, start_response, bucket, key):
        """Upload object or copy object."""
        if bucket != "default":
            return self.send_error(start_response, "404 Not Found", "NoSuchBucket", "Only default bucket")
        
        # Check if this is a CopyObject request
        copy_source = environ.get("HTTP_X_AMZ_COPY_SOURCE")
        if copy_source:
             from urllib.parse import unquote
             # Parse copy source: /bucket/key or bucket/key
             copy_source = unquote(copy_source).lstrip("/")
             if "/" in copy_source:
                 src_bucket, src_key = copy_source.split("/", 1)
             else:
                 return self.send_error(start_response, "400 Bad Request", "InvalidArgument", "Invalid copy source")
             
             logger.info(f"CopyObject: {src_bucket}/{src_key} -> {bucket}/{key}")
             
             # Resolve source
             src_entry, _, _ = self._resolve_key(src_key)
             if not src_entry:
                 return self.send_error(start_response, "404 Not Found", "NoSuchKey", "Source key not found")
             if src_entry.is_folder:
                 return self.send_error(start_response, "400 Bad Request", "InvalidRequest", "Cannot copy folder")
             
             # Download source to temp file
             download_url = None
             if src_entry.url:
                 if src_entry.url.startswith("http"):
                     download_url = src_entry.url
                 else:
                     base = self.client.api_url.replace("/api/v1", "")
                     download_url = f"{base}/{src_entry.url}"
             if not download_url:
                 download_url = self.client.get_download_url(src_entry.id)
             
             headers = {"Authorization": f"Bearer {self.client.api_key}"}
             
             try:
                 response = httpx.get(download_url, headers=headers, follow_redirects=True, timeout=300)
                 if response.status_code != 200:
                     return self.send_error(start_response, "500 Internal Error", "CopyFailed", "Download failed")
                 
                 data = response.content
                 
                 # Calculate MD5
                 file_md5 = hashlib.md5(data)
                 md5_hex = file_md5.hexdigest()
                 
                 # Save to temp file
                 with tempfile.NamedTemporaryFile(delete=False, prefix="s3copy_") as tmp:
                     tmp.write(data)
                     tmp_path = tmp.name
                 
                 # Ensure destination folder exists
                 dirname = os.path.dirname(key)
                 basename = os.path.basename(key)
                 
                 try:
                     parent_id = self._ensure_folder(dirname) if dirname else None
                 except:
                     parent_id = None
                 
                 # Rename temp file
                 final_path = os.path.join(os.path.dirname(tmp_path), basename)
                 shutil.move(tmp_path, final_path)
                 
                 # Delete existing destination if any
                 existing, _, _ = self._resolve_key(key)
                 if existing:
                     self.client.delete_files([existing.id])
                 
                 # Upload
                 rel_path = key if parent_id is None else basename
                 resp = self.client.upload_file(Path(final_path), rel_path, parent_id)
                 
                 # Store MD5
                 uploaded_id = None
                 if isinstance(resp, dict):
                     if "file" in resp:
                         uploaded_id = resp["file"].get("id")
                     elif "fileEntry" in resp:
                         uploaded_id = resp["fileEntry"].get("id")
                 
                 if uploaded_id:
                     self.metadata[str(uploaded_id)] = md5_hex
                     self._save_metadata()
                 
                 os.unlink(final_path)
                 
                 # Return CopyObject response
                 etag = f'"{md5_hex}"'
                 copy_result = {
                     "ETag": etag,
                     "LastModified": format_iso_date(src_entry.updated_at),
                 }
                 xml = create_xml_response("CopyObjectResult", copy_result)
                 return self.send_response(start_response, "200 OK", xml)
                 
             except Exception as e:
                 logger.exception("Copy error")
                 return self.send_error(start_response, "500 Internal Error", "CopyFailed", str(e))

        
        try:
            content_length = int(environ.get("CONTENT_LENGTH", 0))
        except:
            content_length = 0
        
        dirname = os.path.dirname(key)
        basename = os.path.basename(key)
        
        # Ensure parent folder exists
        try:
            parent_id = self._ensure_folder(dirname) if dirname else None
        except Exception as e:
            if "422" in str(e):
                parent_id = None  # Fallback to relativePath
            else:
                return self.send_error(start_response, "500 Internal Error", "FolderError", str(e))
        
        try:
            file_md5 = hashlib.md5()
            is_chunked = environ.get("HTTP_X_AMZ_CONTENT_SHA256") == "STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
            
            with tempfile.NamedTemporaryFile(delete=False, prefix="s3_") as tmp:
                stream = environ["wsgi.input"]
                
                # Ensure stream supports readline for chunked decoding
                if not hasattr(stream, 'readline'):
                    stream = io.BufferedReader(stream)
                
                if is_chunked:
                    # Decode AWS chunked stream
                    while True:
                        line = stream.readline(1024)
                        if not line:
                            break
                        try:
                            hex_size = line.decode("ascii").split(";")[0].strip()
                            chunk_size = int(hex_size, 16)
                        except:
                            break
                        if chunk_size == 0:
                            break
                        data = stream.read(chunk_size)
                        file_md5.update(data)
                        tmp.write(data)
                        stream.read(2)  # \r\n
                elif content_length > 0:
                    left = content_length
                    while left > 0:
                        data = stream.read(min(left, 8192))
                        if not data:
                            break
                        file_md5.update(data)
                        tmp.write(data)
                        left -= len(data)
                else:
                    while True:
                        data = stream.read(8192)
                        if not data:
                            break
                        file_md5.update(data)
                        tmp.write(data)
                
                tmp_path = tmp.name
            
            # Rename to correct filename
            final_path = os.path.join(os.path.dirname(tmp_path), basename)
            shutil.move(tmp_path, final_path)
            
            # Delete existing file if any
            existing, _, _ = self._resolve_key(key)
            if existing:
                self.client.delete_files([existing.id])
            
            # Upload
            rel_path = key if parent_id is None else basename
            resp = self.client.upload_file(Path(final_path), rel_path, parent_id)
            
            # Store MD5
            md5_hex = file_md5.hexdigest()
            uploaded_id = None
            if isinstance(resp, dict):
                if "file" in resp:
                    uploaded_id = resp["file"].get("id")
                elif "fileEntry" in resp:
                    uploaded_id = resp["fileEntry"].get("id")
            
            if uploaded_id:
                self.metadata[str(uploaded_id)] = md5_hex
                self._save_metadata()
            
            os.unlink(final_path)
            
            etag = f'"{md5_hex}"'
            return self.send_response(start_response, "200 OK", b"", headers=[("ETag", etag)])
        
        except Exception as e:
            logger.exception("Upload error")
            return self.send_error(start_response, "500 Internal Error", "UploadFailed", str(e))
    
    def handle_delete_object(self, environ, start_response, bucket, key):
        """Delete object."""
        entry, _, _ = self._resolve_key(key)
        if not entry:
            return self.send_response(start_response, "204 No Content", b"")
        
        try:
            self.client.delete_files([entry.id])
            return self.send_response(start_response, "204 No Content", b"")
        except Exception as e:
            return self.send_error(start_response, "500 Internal Error", "DeleteFailed", str(e))

    def handle_head_object(self, environ, start_response, bucket, key):
        """Get object metadata."""
        entry, _, _ = self._resolve_key(key)
        if not entry:
            return self.send_error(start_response, "404 Not Found", "NoSuchKey", "Key not found", include_body=False)
        
        # Format Last-Modified as RFC 2822
        last_modified = format_http_date(entry.updated_at)
        
        headers = [
            ("Content-Type", entry.mime or "application/octet-stream"),
            ("Content-Length", str(entry.file_size)),
            ("Last-Modified", last_modified),
            ("ETag", self._get_etag(entry)),
        ]
        start_response("200 OK", headers)
        return []


