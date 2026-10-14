"""The publisher's tests reach their "remote" as a bare repository on disk.

Since 14 October 2026 the publisher refuses a remote that is not an https or
ssh address (``forge.publisher.credential.the_remote_transport``), exactly as
its settings do. These tests build their routes by hand around a bare
repository in ``tmp_path``, so for them alone an absolute local folder counts
as a remote, reached over the one ``file`` transport. Production code has no
such path.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _a_local_folder_is_a_test_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    from forge.publisher import git_work

    real = git_work.the_remote_transport

    def transport(address: str):  # noqa: ANN202
        text = str(address or "").strip()
        if text.startswith("/") and "://" not in text:
            return "file"
        return real(text)

    monkeypatch.setattr(git_work, "the_remote_transport", transport)
