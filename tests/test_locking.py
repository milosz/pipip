from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from argus.locking import FileLock, LockError


def test_lock_is_exclusive_and_reusable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "nested" / "argus.lock"
    first = FileLock(path)
    first.acquire()
    try:
        assert path.read_text() == f"{os.getpid()}\n"
        with pytest.raises(LockError):
            FileLock(path).acquire()
    finally:
        first.release()

    second = FileLock(path)
    second.acquire()
    second.release()


def test_lock_release_without_acquire_is_noop(tmp_path) -> None:  # type: ignore[no-untyped-def]
    FileLock(tmp_path / "argus.lock").release()


def test_lock_closes_descriptor_if_pid_sync_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "argus.lock"
    lock = FileLock(path)
    descriptors: list[int] = []

    def fail_sync(fd: int) -> None:
        descriptors.append(fd)
        raise OSError(errno.EIO, "cannot flush PID")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_sync)
        with pytest.raises(OSError, match="cannot flush PID"):
            lock.acquire()

    assert not lock.acquired
    (fd,) = descriptors
    with pytest.raises(OSError) as result:
        os.fstat(fd)
    assert result.value.errno == errno.EBADF
    with FileLock(path) as retry:
        assert retry.acquired
