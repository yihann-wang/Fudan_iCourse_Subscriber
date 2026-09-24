"""Cheap truncation checks for ISO BMFF media, without reading media payloads."""

import os
from pathlib import Path


class IncompleteMediaError(RuntimeError):
    """A local media container is truncated or has an invalid box boundary."""


def check_mp4_completeness(path):
    """Check top-level box boundaries in recognizable MP4/MOV files.

    This is not a full decoder validation: size-zero boxes extend to EOF and
    payload corruption still needs decoding. Other containers are left to
    ffprobe, including legacy files with a misleading .mp4 extension. Seek over
    all payloads so reusing semester-sized recordings stays inexpensive.
    """
    with Path(path).open("rb") as stream:
        actual = os.fstat(stream.fileno()).st_size
        first = stream.read(8)
        if len(first) < 8 or first[4:] not in {
            b"ftyp", b"styp", b"moov", b"moof", b"mdat", b"wide", b"free", b"skip",
        }:
            return
        offset = 0
        while offset < actual:
            stream.seek(offset)
            header = stream.read(8)
            if len(header) != 8:
                raise IncompleteMediaError("录像文件结构不完整；请重新下载这节课。")
            size = int.from_bytes(header[:4], "big")
            header_size = 8
            if size == 1:
                extended = stream.read(8)
                if len(extended) != 8:
                    raise IncompleteMediaError("录像文件结构不完整；请重新下载这节课。")
                size = int.from_bytes(extended, "big")
                header_size += 8
            elif size == 0:
                size = actual - offset
            if header[4:] == b"uuid":
                header_size += 16
            if size < header_size:
                raise IncompleteMediaError("录像文件结构损坏；请重新下载这节课。")
            expected = offset + size
            if expected > actual:
                raise IncompleteMediaError(
                    f"录像未下载完整：现有 {actual / 1e9:.2f} GB，"
                    f"文件头要求至少 {expected / 1e9:.2f} GB。"
                    "请使用“下载并生成笔记”重试，程序会重新下载这节课。"
                )
            offset = expected
