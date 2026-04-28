import sys
import unittest
from pathlib import Path

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class AcquisitionCoreTests(unittest.TestCase):
    def test_run_single_acquisition_relays_frame_progress_from_camera_read(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        class FakeCamera:
            def __init__(self):
                self.disarmed = False

            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                self.disarmed = True

            def read_frame_sequence(self, frame_count, pattern_files, laser_wavelength_nm, frame_callback):
                timestamps = []
                for index in range(1, frame_count + 1):
                    timestamp = float(index)
                    timestamps.append(timestamp)
                    frame_callback(index, timestamp)
                return np.ones((frame_count, 2, 3), dtype=np.uint16), timestamps

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        class FakeDaq:
            def __init__(self):
                self.reset_devices = []

            def play_waveform(self, device_name, plan):
                return None

            def set_all_low(self, device_name):
                self.reset_devices.append(device_name)

        camera = FakeCamera()
        daq = FakeDaq()
        statuses = []

        batch = run_single_acquisition(
            task=SimTaskConfig(),
            daq_config=DaqLineConfig(),
            pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
            camera=camera,
            slm=FakeSlm(),
            daq=daq,
            task_id="progress-test",
            on_status=lambda state, payload: statuses.append((state, payload)),
        )

        self.assertEqual(batch.stack.shape, (9, 2, 3))
        self.assertTrue(camera.disarmed)
        self.assertEqual(daq.reset_devices, ["Dev1"])
        self.assertEqual([payload["frame_index"] for state, payload in statuses if state == "frame_captured"], list(range(1, 10)))

    def test_run_single_acquisition_disarms_camera_when_daq_play_fails(self):
        from sim_control.acquisition_core import run_single_acquisition
        from sim_control.models import DaqLineConfig, PatternPreparationResult, SimTaskConfig

        class FakeCamera:
            def __init__(self):
                self.disarm_calls = 0

            def apply_config(self, config):
                return {}

            def arm(self, frame_count):
                return None

            def disarm(self):
                self.disarm_calls += 1

        class FakeSlm:
            def activate_prepared_patterns(self):
                return None

        class FakeDaq:
            def __init__(self):
                self.reset_calls = 0

            def play_waveform(self, device_name, plan):
                raise RuntimeError("daq failed")

            def set_all_low(self, device_name):
                self.reset_calls += 1

        camera = FakeCamera()
        daq = FakeDaq()

        with self.assertRaisesRegex(RuntimeError, "daq failed"):
            run_single_acquisition(
                task=SimTaskConfig(),
                daq_config=DaqLineConfig(),
                pattern_result=PatternPreparationResult(pattern_files=["p"] * 9, handles=list(range(9))),
                camera=camera,
                slm=FakeSlm(),
                daq=daq,
            )

        self.assertEqual(camera.disarm_calls, 1)
        self.assertEqual(daq.reset_calls, 1)


if __name__ == "__main__":
    unittest.main()
