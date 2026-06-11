import unittest

from alicat_mfc import AlicatMFC


class FakeSerial:
    is_open = True

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.writes = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        return self.response


class AlicatMFCTest(unittest.TestCase):
    def test_tx_rx_callbacks_observe_serial_traffic(self) -> None:
        tx_events = []
        rx_events = []
        mfc = AlicatMFC(
            "COM_FAKE",
            unit_id="A",
            tx_callback=tx_events.append,
            rx_callback=rx_events.append,
        )
        fake_serial = FakeSerial(
            b"A +15.542 +24.57 +16.667 +15.444 +0.25 22741.4 N2\r"
        )
        mfc._ser = fake_serial

        response = mfc.set_setpoint(0.25)

        self.assertEqual(tx_events, ["AS 0.25"])
        self.assertEqual(
            fake_serial.writes,
            [b"AS 0.25\r"],
        )
        self.assertEqual(
            rx_events,
            ["A +15.542 +24.57 +16.667 +15.444 +0.25 22741.4 N2"],
        )
        self.assertEqual(response, rx_events[0])


if __name__ == "__main__":
    unittest.main()
