"""Minimal Drime API client for S3 Gateway."""

import os
import logging
from pathlib import Path
from typing import Optional, List, Any

import httpx

from .models import FileEntry

logger = logging.getLogger(__name__)


class DrimeClient:
    """Minimal Drime Cloud API client."""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("DRIME_API_KEY", "")
        self.api_url = "https://app.drime.cloud/api/v1"
        self.timeout = 120.0
        
        if not self.api_key:
            raise ValueError("DRIME_API_KEY environment variable required")
    
    def _request(self, method: str, endpoint: str, retries: int = 5, **kwargs) -> httpx.Response:
        """Make authenticated API request with retry logic."""
        import time
        
        url = f"{self.api_url}{endpoint}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.api_key}"
        
        last_error = None
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    response = client.request(method, url, headers=headers, **kwargs)
                    response.raise_for_status()
                    return response
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500:
                    last_error = e
                    logger.warning(f"API returned {e.response.status_code}, retry {attempt + 1}/{retries}")
                    time.sleep(0.5 * (attempt + 1))  # Exponential backoff
                    continue
                raise
            except httpx.RequestError as e:
                last_error = e
                logger.warning(f"Request failed: {e}, retry {attempt + 1}/{retries}")
                time.sleep(0.5 * (attempt + 1))
                continue
        
        raise last_error
    
    def list_folder(self, folder_id: Optional[int] = None, per_page: int = 100) -> List[FileEntry]:
        """List all entries in a folder."""
        entries = []
        page = 1
        
        while True:
            params = {
                "workspaceId": 0,
                "perPage": per_page,
                "page": page,
            }
            if folder_id is not None:
                params["parentIds"] = str(folder_id)
            
            response = self._request("GET", "/drive/file-entries", params=params)
            data = response.json()
            
            items = data.get("data", [])
            if not items:
                break
                
            for item in items:
                entries.append(FileEntry.from_dict(item))
            
            # Check pagination
            last_page = data.get("last_page", 1)
            if page >= last_page:
                break
            page += 1
        
        return entries
    
    def create_folder(self, name: str, parent_id: Optional[int] = None) -> dict:
        """Create a new folder."""
        data = {"name": name, "workspaceId": 0}
        if parent_id is not None:
            data["parentId"] = parent_id
        
        response = self._request("POST", "/folders", json=data)
        return response.json()
    
    def upload_file(
        self, 
        file_path: Path, 
        relative_path: str,
        parent_id: Optional[int] = None
    ) -> dict:
        """Upload a file."""
        mime_type = "application/octet-stream"
        
        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, mime_type)}
            data = {
                "relativePath": relative_path,
                "workspaceId": "0",
            }
            if parent_id is not None:
                data["parentId"] = str(parent_id)
            
            response = self._request("POST", "/uploads", files=files, data=data)
            return response.json()
    
    def get_download_url(self, entry_id: int) -> str:
        """Get download URL for a file."""
        return f"{self.api_url}/file-entries/{entry_id}/download"
    
    def delete_files(self, entry_ids: List[int]) -> dict:
        """Delete files permanently."""
        data = {
            "entryIds": entry_ids,
            "deleteForever": True,
        }
        response = self._request("POST", "/file-entries/delete", json=data)
        return response.json()
