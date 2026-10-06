"""Writing a file that another process is reading.

config.json is written by the web backend and read by the engine, which polls
it and reloads on a change. `open(path, "w")` empties a file before it fills
it, so a reader that looks in between gets half a file — and a process killed
in between leaves one behind for good. Losing power during a save is enough.
"""
import errno
import os
import shutil
import threading

# The errors that mean "this file cannot be replaced where it stands", as
# opposed to "something went wrong with the data": no permission to create a
# file beside it, and rename(2) refusing the target. Only these fall back to
# rewriting in place — on a full disk that would empty the file and then fail,
# which is the very thing the temporary file is there to prevent.
_NO_TEMP_FILE = (errno.EACCES, errno.EPERM)
_CANNOT_REPLACE = (errno.EBUSY, errno.EXDEV)


def write_atomically(path: str, text: str) -> None:
    """Replace `path` with `text`; a reader sees the old file or the new one.

    The text goes to a temporary file beside `path`, which is then renamed
    over it. A rename within one directory is a single step to every reader,
    and a write that fails part-way leaves the original as it was.

    Raises OSError when the file could not be written at all.
    """
    # Unique to this writer, so two of them (the backend's request threads, or
    # both processes creating a missing file at first boot) never share one.
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        # 0o666 less the umask: the mode open(path, "w") would have given.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
    except OSError as e:
        if e.errno not in _NO_TEMP_FILE:
            raise
        # The directory is not ours to write in, though the file may be.
        _rewrite_in_place(path, text)
        return
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            _sync(f.fileno())
        _keep_mode(path, tmp)
        os.replace(tmp, path)
    except OSError as e:
        _discard(tmp)
        if e.errno not in _CANNOT_REPLACE:
            raise
        # A file bind-mounted into a container on its own — rather than the
        # directory holding it — is a mount point: it can be rewritten but not
        # renamed over. Saving has to keep working there.
        _rewrite_in_place(path, text)


def _rewrite_in_place(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


def _sync(fd: int) -> None:
    """Ask for the bytes to reach the disk before the rename makes them the
    file. Not every filesystem supports it, and that is no reason to fail."""
    try:
        os.fsync(fd)
    except OSError:
        pass


def _keep_mode(path: str, tmp: str) -> None:
    """Carry an existing file's permissions over to its replacement."""
    try:
        shutil.copymode(path, tmp)
    except OSError:
        pass                # nothing there yet — the new file's mode stands


def _discard(tmp: str) -> None:
    try:
        os.unlink(tmp)
    except OSError:
        pass
