import threading
import unittest

from outgoing_registry import OutgoingMessageRegistry


class OutgoingMessageRegistryTest(unittest.TestCase):
    def test_cancelled_reservation_does_not_hide_an_incoming_message(self):
        registry = OutgoingMessageRegistry()
        reservation = registry.reserve("项目群", "相同内容")

        registry.cancel(reservation)

        self.assertFalse(registry.should_ignore("项目群", "相同内容"))

    def test_identical_serial_sends_each_consume_one_echo(self):
        registry = OutgoingMessageRegistry()
        registry.reserve("项目群", "收到")
        registry.reserve("项目群", "收到")

        self.assertTrue(registry.should_ignore("项目群", "收到"))
        self.assertTrue(registry.should_ignore("项目群", "收到"))
        self.assertFalse(registry.should_ignore("项目群", "收到"))

    def test_record_and_consume_are_thread_safe(self):
        registry = OutgoingMessageRegistry()
        barrier = threading.Barrier(3)
        results = []

        def producer():
            barrier.wait()
            for index in range(100):
                registry.reserve("项目群", f"message-{index}")

        def consumer():
            barrier.wait()
            matched = set()
            while len(matched) < 100:
                for index in range(100):
                    if index not in matched and registry.should_ignore(
                        "项目群", f"message-{index}"
                    ):
                        matched.add(index)
            results.append(len(matched))

        producer_thread = threading.Thread(target=producer)
        consumer_thread = threading.Thread(target=consumer)
        producer_thread.start()
        consumer_thread.start()
        barrier.wait()
        producer_thread.join(2)
        consumer_thread.join(2)

        self.assertFalse(producer_thread.is_alive())
        self.assertFalse(consumer_thread.is_alive())
        self.assertEqual(results, [100])


if __name__ == "__main__":
    unittest.main()
