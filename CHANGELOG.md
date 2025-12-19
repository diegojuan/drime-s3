# Changelog

All notable changes to the Drime S3 Gateway project will be documented in this file.

## [Unreleased] - 2025-12-19

### Added
- **Native S3 Multipart Upload Support**: Implemented transparent proxying for S3 multipart uploads. The gateway now handles `Initiate Multipart Upload`, `Upload Part`, `Complete Multipart Upload`, and `Abort Multipart Upload` operations.
- **Direct Streaming to R2**: Implemented a streaming architecture that proxies part uploads directly to presigned S3/R2 URLs provided by the Drime API. This ensures **zero local disk usage** for staging, significantly improving performance and reducing resource requirements for large files.
- **Part Size Tracking**: Added logic to track the size of individual uploaded parts in memory. This allows the gateway to correctly calculate and report the final file size to the Drime API upon completion, resolving specific compatibility issues with clients like **Rclone** that may not furnish the total file size during initialization.
- **Composite Upload ID**: Introduced a Base64-encoded composite ID scheme (wrapping Drime's `uploadId` and `key`) to map stateful Drime upload sessions to stateless S3 `UploadId` tokens.

### Fixed
- **HTTP HEAD Protocol Compliance**: Fixed an issue where `HEAD` requests for non-existent keys returned an XML error body. The gateway now correctly returns a 404 status with an empty body, eliminating "Unsolicited response" warnings in clients like Rclone.
- **Restic Compatibility (Tempfile Error)**: Resolved an `UnboundLocalError` in `handle_put_object` caused by a shadowed `tempfile` import. This restores full functionality for single-file uploads used by tools like Restic.
- **Request Routing**: Improved the WSGI request dispatcher to properly identify and route `POST` requests with query parameters (e.g., `?uploads`), fixing `InvalidURI` errors during multipart initiation.
