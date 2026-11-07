"""Safe local files: reject links, keep writes atomic, and constrain permissions."""

from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import stat


class UnsafeFileError(RuntimeError):
    """A path or file cannot be accessed without weakening the file contract."""


def absolute_path(path: Path) -> Path:
    """Normalize a caller path without resolving links or permitting '..'."""
    path = path.expanduser().absolute()
    if ".." in path.parts:
        raise UnsafeFileError("file paths must not contain '..'")
    return path


@contextmanager
def _parent_directory(path: Path, *, create: bool = False):
    # Walk from the root using directory descriptors. Do not resolve symlinks:
    # even a parent swapped after validation must not redirect reads or writes.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _require_regular(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise UnsafeFileError("files must be regular files without links")


def read_optional(path: Path) -> str | None:
    """Read UTF-8 or return None when missing; reject links in the entire path."""
    path = absolute_path(path)
    try:
        with _parent_directory(path) as directory:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                _require_regular(os.fstat(handle.fileno()))
                return handle.read()
    except FileNotFoundError:
        return None


def write_atomic(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Replace a regular file atomically with explicit permissions and no links."""
    path = absolute_path(path)
    with _parent_directory(path, create=True) as directory:
        try:
            _require_regular(os.stat(path.name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            pass
        temporary = f".{path.name}.{secrets.token_hex(16)}.partial"
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(content)
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def regular_file(path: Path) -> bool:
    """Return False only for missing files; reject links and nonregular files."""
    path = absolute_path(path)
    try:
        with _parent_directory(path) as parent:
            metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _require_regular(metadata)
        return True
    except FileNotFoundError:
        return False


def ensure_private_parent(path: Path) -> None:
    """Create or restrict the immediate parent without following directory links."""
    with _parent_directory(absolute_path(path), create=True) as directory:
        os.fchmod(directory, 0o700)
