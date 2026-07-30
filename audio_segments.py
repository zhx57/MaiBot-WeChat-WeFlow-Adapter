"""微信语音出站使用的 MP3/WAV 时长检测与无损分段。

这里刻意不调用 ffmpeg 等外部解码器：WAV 按 PCM 帧切分，MP3 按完整音频帧
切分，因此不会在帧中间截断。返回的临时分段由调用方在发送结束后负责清理。
"""

from __future__ import annotations

import contextlib
import math
import os
import tempfile
import wave


class AudioSplitError(ValueError):
    """音频容器无法可靠读取，或无法在不重新编码的情况下安全分段。"""


def split_audio_file(path, max_seconds=58.0):
    """返回一组不超过 ``max_seconds`` 的可播放音频路径。

    原文件未超长时直接返回原路径；发生分段时返回新建临时文件，所有权归调用方。
    """
    max_seconds = float(max_seconds)
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be positive")
    with open(path, "rb") as stream:
        header = stream.read(16)
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return _split_wav(path, max_seconds)
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
    ):
        return _split_mp3(path, max_seconds)
    raise AudioSplitError("only MP3 and WAV voice files are supported")


def audio_duration_seconds(path):
    """不启动外部解码器，直接从 MP3/WAV 帧信息计算时长。"""
    with open(path, "rb") as stream:
        data = stream.read()
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        try:
            with wave.open(path, "rb") as reader:
                return reader.getnframes() / float(reader.getframerate())
        except (OSError, EOFError, wave.Error) as exc:
            raise AudioSplitError(f"invalid WAV audio: {exc}") from exc
    return sum(duration for _start, _end, duration in _mp3_frames(data))


def _temporary_path(suffix):
    fd, path = tempfile.mkstemp(prefix="wemai_voice_part_", suffix=suffix)
    os.close(fd)
    return path


def _remove_paths(paths):
    for path in paths:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _split_wav(path, max_seconds):
    try:
        with wave.open(path, "rb") as reader:
            params = reader.getparams()
            frame_rate = reader.getframerate()
            frame_count = reader.getnframes()
            if frame_rate <= 0:
                raise AudioSplitError("WAV sample rate is invalid")
            if frame_count / float(frame_rate) <= max_seconds:
                return [path]
            if reader.getcomptype() != "NONE":
                raise AudioSplitError("compressed WAV voice files cannot be split")

            frames_per_part = max(1, int(frame_rate * max_seconds))
            paths = []
            try:
                while reader.tell() < frame_count:
                    raw = reader.readframes(frames_per_part)
                    if not raw:
                        break
                    part_path = _temporary_path(".wav")
                    paths.append(part_path)
                    with wave.open(part_path, "wb") as writer:
                        writer.setparams(params)
                        writer.writeframes(raw)
            except BaseException:
                _remove_paths(paths)
                raise
    except AudioSplitError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise AudioSplitError(f"invalid WAV audio: {exc}") from exc
    if not paths:
        raise AudioSplitError("WAV audio contains no frames")
    return paths


def _syncsafe_size(raw):
    if len(raw) != 4 or any(value & 0x80 for value in raw):
        raise AudioSplitError("invalid MP3 ID3 header")
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def _mp3_frame_info(data, offset):
    if offset + 4 > len(data):
        return None
    header = int.from_bytes(data[offset : offset + 4], "big")
    if header & 0xFFE00000 != 0xFFE00000:
        return None
    version_id = (header >> 19) & 0x3
    layer_id = (header >> 17) & 0x3
    bitrate_index = (header >> 12) & 0xF
    sample_index = (header >> 10) & 0x3
    padding = (header >> 9) & 0x1
    if version_id == 1 or layer_id == 0 or bitrate_index in {0, 15} or sample_index == 3:
        return None

    layer = {3: 1, 2: 2, 1: 3}[layer_id]
    sample_rate = (44100, 48000, 32000)[sample_index]
    if version_id == 2:
        sample_rate //= 2
    elif version_id == 0:
        sample_rate //= 4

    if version_id == 3:
        bitrate_tables = {
            1: (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
            2: (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
            3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        }
        samples = 384 if layer == 1 else 1152
    else:
        bitrate_tables = {
            1: (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
            2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
            3: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        }
        samples = 384 if layer == 1 else (1152 if layer == 2 else 576)
    bitrate = bitrate_tables[layer][bitrate_index] * 1000
    if layer == 1:
        frame_size = ((12 * bitrate // sample_rate) + padding) * 4
    elif layer == 3 and version_id != 3:
        frame_size = 72 * bitrate // sample_rate + padding
    else:
        frame_size = 144 * bitrate // sample_rate + padding
    if frame_size < 4 or offset + frame_size > len(data):
        return None
    return frame_size, samples / float(sample_rate)


def _mp3_frames(data):
    if len(data) < 4:
        raise AudioSplitError("MP3 audio is empty")
    start = 0
    if data.startswith(b"ID3"):
        if len(data) < 10:
            raise AudioSplitError("MP3 ID3 header is truncated")
        start = 10 + _syncsafe_size(data[6:10])
        if data[5] & 0x10:
            start += 10
    first = None
    search_end = min(len(data) - 3, start + 65536)
    for offset in range(start, search_end):
        info = _mp3_frame_info(data, offset)
        if info is None:
            continue
        next_offset = offset + info[0]
        if next_offset == len(data) or _mp3_frame_info(data, next_offset) is not None:
            first = offset
            break
    if first is None:
        raise AudioSplitError("MP3 audio contains no valid frames")

    frames = []
    offset = first
    while offset + 4 <= len(data):
        info = _mp3_frame_info(data, offset)
        if info is None:
            trailing = data[offset:]
            if not trailing.strip(b"\x00") or trailing.startswith((b"TAG", b"APETAGEX")):
                break
            raise AudioSplitError("MP3 frame stream is malformed")
        frame_size, duration = info
        frames.append((offset, offset + frame_size, duration))
        offset += frame_size
    if not frames:
        raise AudioSplitError("MP3 audio contains no valid frames")
    return frames


def _split_mp3(path, max_seconds):
    with open(path, "rb") as stream:
        data = stream.read()
    frames = _mp3_frames(data)
    if sum(frame[2] for frame in frames) <= max_seconds:
        return [path]

    # A Xing/Info/VBRI metadata frame can advertise the original full duration.
    # Dropping that non-audio frame keeps each standalone part self-describing.
    first_bytes = data[frames[0][0] : frames[0][1]]
    if any(marker in first_bytes[:256] for marker in (b"Xing", b"Info", b"VBRI")):
        frames = frames[1:]
    if not frames:
        raise AudioSplitError("MP3 audio contains no playable frames")

    groups = []
    current = []
    current_duration = 0.0
    for frame in frames:
        if current and current_duration + frame[2] > max_seconds:
            groups.append(current)
            current = []
            current_duration = 0.0
        current.append(frame)
        current_duration += frame[2]
    if current:
        groups.append(current)

    paths = []
    try:
        for group in groups:
            part_path = _temporary_path(".mp3")
            paths.append(part_path)
            with open(part_path, "wb") as stream:
                stream.write(data[group[0][0] : group[-1][1]])
    except BaseException:
        _remove_paths(paths)
        raise
    return paths
