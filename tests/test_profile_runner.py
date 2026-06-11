import tempfile
import unittest
from pathlib import Path

from alicat_mfc import MFCReading
from profile_runner import (
    ProfileRunner,
    ProfileRunnerConfig,
    exponential_decay_profile,
    gaussian_profile,
)


class FakeMFC:
    def __init__(self) -> None:
        self.is_connected = True
        self.commands = []

    def connect(self) -> None:
        self.is_connected = True

    def set_setpoint(self, value: float) -> str:
        self.commands.append(value)
        return f"A setpoint {value}"

    def zero_flow(self) -> str:
        return self.set_setpoint(0.0)

    def poll(self) -> MFCReading:
        return MFCReading(
            raw="A +15.0 +24.0 +0.1 +0.1 +0.1 0.0 N2",
            unit_id="A",
            abs_pressure=15.0,
            temperature=24.0,
            volumetric_flow=0.1,
            mass_flow=0.1,
            setpoint=0.1,
            totalizer=0.0,
            gas="N2",
        )


class ProfileRunnerTest(unittest.TestCase):
    def test_profile_shapes(self) -> None:
        gaussian = gaussian_profile(
            baseline=0.1,
            amplitude=0.4,
            center_s=10.0,
            sigma_s=2.0,
        )
        decay = exponential_decay_profile(start=1.0, tau_s=2.0, baseline=0.1)

        self.assertAlmostEqual(gaussian(10.0), 0.5)
        self.assertAlmostEqual(decay(0.0), 1.1)
        self.assertLess(decay(4.0), decay(1.0))

    def test_runner_stop_zeroes_and_writes_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = FakeMFC()
            log_path = Path(tmpdir) / "run.csv"
            config = ProfileRunnerConfig(
                duration_s=5.0,
                control_period_s=0.01,
                poll_period_s=0.01,
                settle_s=0.0,
                zero_on_start=False,
                zero_on_finish=True,
                log_path=log_path,
            )

            runner_holder = {}

            def on_progress(_row) -> None:
                runner_holder["runner"].request_stop()

            runner = ProfileRunner(
                fake,
                lambda _t: 0.5,
                config,
                progress_callback=on_progress,
            )
            runner_holder["runner"] = runner

            rows = runner.run()

            self.assertTrue(rows)
            self.assertIn("stop_requested", [row.event for row in rows])
            self.assertEqual(fake.commands[-1], 0.0)
            self.assertTrue(log_path.exists())
            self.assertIn("q_commanded", log_path.read_text())


if __name__ == "__main__":
    unittest.main()
