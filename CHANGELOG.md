# Changelog

Notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-08-14

### Added

- Add `walk()` and path-aware `glob()` for recursive object discovery.
- Add managed single-object `copy()` and copy-then-delete `move()` operations.
- Add `rename()` for copy-then-delete object and directory-prefix renames.
- Use native `RenameObject` for AWS S3 Express directory buckets and expose
  explicit `auto`, `native`, and `copy` rename strategies.
- Run S3 Express directory-prefix renames concurrently with a configurable
  `max_workers` limit.

## [0.1.0] - 2026-08-13

### Added

- Package `s3os` as an installable Python project.
- Add `S3OS` helpers for path handling, object IO, listing, upload/download,
  and deletion.
- Add read-only and optional read/write bucket permission checks.
- Add `Content-MD5` support for S3-compatible `DeleteObjects` requests.
- Add tests, CI, and a safer public example.
