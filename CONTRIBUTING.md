# Contributing

Thanks for taking a look at `s3os`.

## Local Setup

```bash
git clone https://github.com/alivecat05/s3os.git
cd s3os
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest
python -m pip install build twine
python -m build
python -m twine check dist/*
```

## Pull Requests

Please keep changes small and focused. Good pull requests usually include:

- A short explanation of the S3 behavior being changed.
- Tests for path handling, listing, delete behavior, or error behavior.
- README updates when the public API changes.

## Design Notes

`s3os` should stay small. It is not trying to replace `boto3`, `s3fs`, or
`fsspec`; it is a thin convenience layer for scripts and applications that want
simple os-like operations while keeping direct boto3 control.

Deletion behavior should be conservative. Recursive delete operations must not
operate on the bucket root and must use an exact `prefix/` boundary.

## Reporting Bugs

Open a GitHub issue with a minimal reproduction, Python version, boto3 version,
and the S3 provider involved. Never include access keys, secret keys, session
tokens, private endpoint credentials, or sensitive object names.
