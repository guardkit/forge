"""Read a build's output one line at a time, however long a line is.

19 October 2026, build FEAT-B874. The runner read GuardKit's output with
``await proc.stdout.readline()``. asyncio's stream reader refuses a line
longer than its buffer limit (64 KiB by default): ``readline`` raises
``ValueError('Separator is not found, and chunk exceed the limit')``. One
long line from the build (an npm cache listing in the worktree) therefore
raised out of the runner's ``running_wave`` node, the LangGraph run ended in
an error, and the build died with no terminal and no failure pack.

:class:`OutputLines` reads the stream in fixed-size chunks with
``read(n)``, which has no line limit, and splits on newlines itself. A line
longer than the cap keeps its first ``cap`` bytes and is marked
``… [line cut: N bytes]``, where ``N`` is the number of bytes not kept. Every
other byte reaches the caller exactly as ``readline`` delivered it, so the
stdout log, the build monitor and every parser see the same text as before.

The reader holds the only copy of a partial line, so one reader must serve
the whole life of the stream: the runner's main drain and, after a timeout
kill, its tail read share it. A cancel arriving while the reader waits on
``read`` loses nothing, because bytes are moved into the reader only after a
read returns.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "OUTPUT_LINE_CAP_BYTES",
    "OUTPUT_READ_CHUNK_BYTES",
    "OutputLines",
    "cut_marker",
]

#: The longest line kept whole, in bytes. Anything longer keeps this many
#: bytes and is marked as cut. 64 KiB is the limit asyncio's own ``readline``
#: used, so no line that reached the log before is shortened now.
OUTPUT_LINE_CAP_BYTES: int = 64 * 1024

#: How much is asked of the stream at a time.
OUTPUT_READ_CHUNK_BYTES: int = 64 * 1024


def cut_marker(dropped: int) -> str:
    """The plain marker appended to a line that was cut."""
    return f"… [line cut: {dropped} bytes]"


def _decode(raw: bytes) -> str:
    # The same treatment the runner always gave a readline() result.
    return raw.decode("utf-8", errors="replace").rstrip()


class OutputLines:
    """Lines from an asyncio byte stream, with no limit on line length.

    ``stream`` needs only an awaitable ``read(n)`` that returns ``b""`` at the
    end of the stream (an :class:`asyncio.StreamReader` qualifies).

    :meth:`next_line` returns each line decoded as UTF-8 (undecodable bytes
    replaced) with trailing whitespace removed, as the runner's drain did with
    ``readline``; a blank line is ``""``. It returns ``None`` at the end of the
    stream, and after that it never reads the stream again.
    """

    def __init__(
        self,
        stream: Any,
        *,
        cap: int = OUTPUT_LINE_CAP_BYTES,
        chunk: int = OUTPUT_READ_CHUNK_BYTES,
    ) -> None:
        if cap < 1 or chunk < 1:
            raise ValueError("cap and chunk must be positive")
        self._stream = stream
        self._cap = cap
        self._chunk = chunk
        self._pending = bytearray()
        # The kept start of a line that is being cut, and how many of its
        # bytes have been dropped so far. ``None`` when no line is being cut.
        self._kept: bytes | None = None
        self._dropped = 0
        self._eof = False
        #: How many lines this reader has cut (for a log line or a test).
        self.lines_cut = 0

    async def next_line(self) -> str | None:
        while True:
            line = self._take_line()
            if line is not None:
                return line
            if self._eof:
                return self._take_last()
            data = await self._stream.read(self._chunk)
            if not data:
                self._eof = True
                continue
            self._pending += data

    def _take_line(self) -> str | None:
        end = self._pending.find(b"\n")
        if end >= 0:
            raw = bytes(self._pending[:end])
            del self._pending[: end + 1]
            return self._finish(raw)
        # No newline yet. Hold at most ``cap`` bytes of one line.
        if self._kept is not None:
            self._dropped += len(self._pending)
            self._pending.clear()
        elif len(self._pending) > self._cap:
            self._kept = bytes(self._pending[: self._cap])
            self._dropped = len(self._pending) - self._cap
            self._pending.clear()
        return None

    def _take_last(self) -> str | None:
        if not self._pending and self._kept is None:
            return None
        raw = bytes(self._pending)
        self._pending.clear()
        return self._finish(raw)

    def _finish(self, raw: bytes) -> str:
        if self._kept is not None:
            kept, dropped = self._kept, self._dropped + len(raw)
            self._kept, self._dropped = None, 0
        elif len(raw) > self._cap:
            kept, dropped = raw[: self._cap], len(raw) - self._cap
        else:
            return _decode(raw)
        self.lines_cut += 1
        return f"{_decode(kept)}{cut_marker(dropped)}"
