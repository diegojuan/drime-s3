# Changelog

All notable changes to the Drime S3 Gateway project will be documented in this file.


## [1.0.3 xmas edition] - 2025-12-24

### Optimized (Performance)
- **High-Performance Streaming Buffer**: Increased internal streaming buffer size (`STREAM_CHUNK_SIZE`) from 8KB to **1MB**. This significantly reduces CPU overhead and system calls, maximizing throughput for high-speed networks.
- **Smart Metadata Caching**: Implemented a multi-layer caching system for file resolution:
    - **API Cache**: Added a 5-second TTL cache for `list_folder` API calls to prevent "Cache Stampede" during intensive scans (e.g., Restic backup).
    - **Path Resolution Cache**: Added permanent memory caching for resolved directory paths (`_folder_cache`), converting path resolution from recursive O(N) to O(1).
- **High Concurrency Support**: Increased WSGI server thread pool from 10 to **100 threads**. This allows handling high-concurrency workloads (e.g., `restic -o s3.connections=32`) without request queuing.

### Added
- **Range Request Support (Partial Content)**: The Gateway now fully supports the `Range` header for downloads. It propagates the range to the upstream storage (R2) and returns `206 Partial Content`. This fixes corrupted downloads and enables **multi-threaded downloading** and resumption in clients like Rclone and browsers.
- **True Streaming Downloads**: Re-architected `handle_get_object` to use `httpx.stream`. This allows downloading files of any size (e.g., 1TB) with constant, minimal memory usage (~256KB), fixing potential OOM crashes.

### Fixed
- **Redirect Handling (302)**: Fixed an issue where clients (like Restic) received raw `302 Found` responses from the API instead of file content. The Gateway now automatically follows redirects to the final signed URL before streaming data to the client.
- **API Cache Stampede**: Added `threading.Lock` to the metadata cache to ensure thread safety and prevent multiple simultaneous API calls for the same resource.

## [Unreleased] - 2025-12-19
### Added
- **Native S3 Multipart Upload Support**: Implemented transparent proxying for S3 multipart uploads. The gateway now handles `Initiate Multipart Upload`, `Upload Part`, `Complete Multipart Upload`, and `Abort Multipart Upload` operations.
- **Direct Streaming to R2**: Implemented a streaming architecture that proxies part uploads directly to presigned S3/R2 URLs provided by the Drime API. This ensures **zero local disk usage** for staging.
- **Part Size Tracking**: Added logic to track the size of individual uploaded parts in memory to correctly calculate final file size.
- **Composite Upload ID**: Introduced a Base64-encoded composite ID scheme (wrapping Drime's `uploadId` and `key`).

### Fixed
- **HTTP HEAD Protocol Compliance**: Fixed `HEAD` requests returning XML bodies (now returns empty body).
- **Restic Compatibility**: Resolved `UnboundLocalError` in `handle_put_object`.
