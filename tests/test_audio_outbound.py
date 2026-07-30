import asyncio
import contextlib
import os
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from types import MethodType
from unittest.mock import patch

from audio_segments import audio_duration_seconds, split_audio_file
from voice_host_backend import OutboundUIGate
from wx_Processor import (
    MessageProcessor,
    PartialVoiceDeliveryError,
    _VOICE_SEGMENT_SECONDS,
)


def _write_wav(path, seconds, frame_rate=1000):
    with wave.open(path, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(frame_rate)
        writer.writeframes(b"\x80" * int(seconds * frame_rate))


def _write_mp3_frames(path, frame_count):
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz: 417-byte frames, 1152 samples.
    frame = b"\xff\xfb\x90\x64" + (b"\x00" * 413)
    with open(path, "wb") as stream:
        stream.write(frame * frame_count)


class AudioSplitTests(unittest.TestCase):
    def test_wechat_segment_limit_is_sixty_seconds(self):
        self.assertEqual(_VOICE_SEGMENT_SECONDS, 60.0)

    def test_short_wav_is_not_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "short.wav")
            _write_wav(source, 2.0)

            self.assertEqual(split_audio_file(source, 2.0), [source])

    def test_long_wav_is_split_with_final_remainder(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "long.wav")
            _write_wav(source, 5.25)
            parts = split_audio_file(source, 2.0)
            try:
                self.assertEqual(len(parts), 3)
                durations = [audio_duration_seconds(path) for path in parts]
                self.assertEqual(durations, [2.0, 2.0, 1.25])
            finally:
                for path in parts:
                    with contextlib.suppress(OSError):
                        os.unlink(path)

    def test_long_mp3_is_split_only_on_frame_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "long.mp3")
            _write_mp3_frames(source, 200)
            parts = split_audio_file(source, 2.0)
            try:
                self.assertEqual(len(parts), 3)
                durations = [audio_duration_seconds(path) for path in parts]
                self.assertTrue(all(duration <= 2.0 for duration in durations))
                self.assertAlmostEqual(sum(durations), 200 * 1152 / 44100, places=6)
            finally:
                for path in parts:
                    with contextlib.suppress(OSError):
                        os.unlink(path)


class OutboundPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_items_run_before_queued_voice_part(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        observed = []

        async def deliver(_self, _receiver, kind, data):
            observed.append((kind, data))

        processor._deliver_with_retry = MethodType(deliver, processor)
        queue = asyncio.PriorityQueue()
        loop = asyncio.get_running_loop()
        voice_done = loop.create_future()
        text_done = loop.create_future()
        file_done = loop.create_future()
        await queue.put((1, 1, ("chat", "voice", "part-2", voice_done, 1)))
        await queue.put((0, 2, ("chat", "text", "urgent", text_done, 0)))
        await queue.put((0, 3, ("chat", "file", "fast", file_done, 0)))

        task = asyncio.create_task(
            processor._process_receiver_send_queue("chat", queue)
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(text_done, file_done, voice_done), timeout=1
            )
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.assertEqual(
            observed,
            [("text", "urgent"), ("file", "fast"), ("voice", "part-2")],
        )

    async def test_partial_voice_failure_is_marked_non_replayable(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor._cleanup_tasks = set()
        directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, directory.cleanup)
        source = os.path.join(directory.name, "source.wav")
        first = os.path.join(directory.name, "part1.wav")
        second = os.path.join(directory.name, "part2.wav")
        for path in (source, first, second):
            Path(path).write_bytes(b"test")

        async def run_blocking(_self, function, *args, **_kwargs):
            return function(*args)

        async def queue_outbound(_self, _receiver, _kind, data, priority=None):
            self.assertIsNotNone(priority)
            if data == second:
                raise RuntimeError("second part failed")

        processor._run_blocking = MethodType(run_blocking, processor)
        processor._prepare_voice = MethodType(lambda _self, _data: source, processor)
        processor._queue_outbound = MethodType(queue_outbound, processor)

        with patch("wx_Processor.split_audio_file", return_value=[first, second]):
            with self.assertRaises(PartialVoiceDeliveryError) as raised:
                await processor._send_voice_segment("chat", "audio")

        self.assertEqual(raised.exception.sent_parts, 1)
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))

    async def test_cancelled_voice_handler_keeps_active_part_until_delivery_finishes(self):
        processor = MessageProcessor.__new__(MessageProcessor)
        processor._cleanup_tasks = set()
        directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, directory.cleanup)
        source = os.path.join(directory.name, "source.wav")
        active = os.path.join(directory.name, "active.wav")
        later = os.path.join(directory.name, "later.wav")
        for path in (source, active, later):
            Path(path).write_bytes(b"test")
        release_delivery = asyncio.Event()

        async def run_blocking(_self, function, *args, **_kwargs):
            return function(*args)

        async def queue_outbound(_self, _receiver, _kind, _data, priority=None):
            self.assertIsNotNone(priority)
            await release_delivery.wait()

        processor._run_blocking = MethodType(run_blocking, processor)
        processor._prepare_voice = MethodType(lambda _self, _data: source, processor)
        processor._queue_outbound = MethodType(queue_outbound, processor)

        with patch("wx_Processor.split_audio_file", return_value=[active, later]):
            handler = asyncio.create_task(
                processor._send_voice_segment("chat", "audio")
            )
            await asyncio.sleep(0)
            handler.cancel()
            await asyncio.gather(handler, return_exceptions=True)

        self.assertTrue(os.path.exists(active))
        self.assertFalse(os.path.exists(later))
        release_delivery.set()
        await asyncio.gather(*processor._cleanup_tasks)
        self.assertFalse(os.path.exists(active))


class OutboundGateTests(unittest.TestCase):
    def test_waiting_normal_send_wins_at_voice_boundary(self):
        gate = OutboundUIGate()
        order = []
        send_acquired = threading.Event()
        allow_send_release = threading.Event()

        gate.acquire_voice()

        def next_voice():
            gate.acquire_voice()
            order.append("voice")
            gate.release_voice()

        def normal_send():
            gate.acquire_send()
            order.append("send")
            send_acquired.set()
            allow_send_release.wait(1)
            gate.release_send()

        voice_thread = threading.Thread(target=next_voice)
        send_thread = threading.Thread(target=normal_send)
        voice_thread.start()
        send_thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with gate._condition:
                if gate._voice_waiters == 1 and gate._send_waiters == 1:
                    break
            time.sleep(0.005)
        gate.release_voice()
        self.assertTrue(send_acquired.wait(1))
        self.assertEqual(order, ["send"])
        allow_send_release.set()
        send_thread.join(1)
        voice_thread.join(1)
        self.assertEqual(order, ["send", "voice"])


if __name__ == "__main__":
    unittest.main()
