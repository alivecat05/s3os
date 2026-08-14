# Changelog

Notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add `walk()` and path-aware `glob()` for recursive object discovery.
- Add managed single-object `copy()` and copy-then-delete `move()` operations.
- Add `rename()` as an explicit copy-then-delete object rename operation.

## [0.1.0] - 2026-08-13

### Added

- Package `s3os` as an installable Python project.
- Add `S3OS` helpers for path handling, object IO, listing, upload/download,
  and deletion.
- Add read-only and optional read/write bucket permission checks.
- Add `Content-MD5` support for S3-compatible `DeleteObjects` requests.
- Add tests, CI, and a safer public example.
