import unittest
from unittest import mock

from PyQt5.QtCore import QCoreApplication

from control_wangbo.MCUTriggerThread import (
    MCU_CMD_CAPTURE,
    MCU_CMD_FUNCTION,
    MCU_CMD_OFF,
    MCU_CMD_RELEASE,
    MCU_CMD_RELEASE_SORT,
    MCU_CMD_SORT,
    MCU_MAX_TIMED_MS,
    MCU_TIME_CONTINUOUS,
    MCUTriggerWorker,
)


class _FakeSerialPort:
    def __init__(self):
        self.writes = []
        self.clear_count = 0
        self.flush_count = 0

    def clear(self):
        self.clear_count += 1

    def write(self, frame):
        self.writes.append(bytes(frame))

    def flush(self):
        self.flush_count += 1


class MCUTriggerProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_mcu_frame_matches_two_byte_firmware_protocol(self):
        self.assertEqual(MCUTriggerWorker._mcu_frame(MCU_CMD_CAPTURE, 5), bytes([0xFE, 5]))
        self.assertEqual(MCU_CMD_FUNCTION, 0xFD)
        self.assertEqual(MCUTriggerWorker._mcu_frame(MCU_CMD_RELEASE, 10), bytes([0xFB, 10]))
        self.assertEqual(MCUTriggerWorker._mcu_frame(MCU_CMD_SORT, 1), bytes([0xF7, 1]))
        self.assertEqual(MCUTriggerWorker._mcu_frame(MCU_CMD_RELEASE_SORT, 3), bytes([0xF3, 3]))
        self.assertEqual(MCUTriggerWorker._mcu_frame(MCU_CMD_OFF, MCU_TIME_CONTINUOUS), bytes([0xFF, 0xFF]))

    def test_mcu_frame_uses_continuous_time_byte_after_firmware_limit(self):
        self.assertEqual(
            MCUTriggerWorker._mcu_frame(MCU_CMD_RELEASE, MCU_MAX_TIMED_MS),
            bytes([0xFB, 254]),
        )
        self.assertEqual(
            MCUTriggerWorker._mcu_frame(MCU_CMD_RELEASE, MCU_MAX_TIMED_MS + 1),
            bytes([0xFB, 0xFF]),
        )

    def test_function_wait_does_not_send_function_command(self):
        worker = MCUTriggerWorker()
        worker.MCUSerialPort = _FakeSerialPort()
        worker.functionTime = 1

        with mock.patch("control_wangbo.MCUTriggerThread.QTimer.singleShot") as single_shot:
            worker.slot_btn_triggerFunction()

        self.assertEqual(worker.MCUSerialPort.writes, [])
        single_shot.assert_called_once()

    def test_release_sort_sends_release_then_release_sort(self):
        worker = MCUTriggerWorker()
        worker.MCUSerialPort = _FakeSerialPort()
        worker.releaseTime = 10
        worker.releaseSortTime = 3
        worker.send_data_releaseSort = MCUTriggerWorker._mcu_frame(MCU_CMD_RELEASE_SORT, 3)

        callbacks = []

        def _capture_timer(_delay_ms, _timer_type, callback):
            callbacks.append(callback)

        with mock.patch("control_wangbo.MCUTriggerThread.QTimer.singleShot", side_effect=_capture_timer):
            worker.slot_btn_triggerReleaseSort()
            self.assertEqual(worker.MCUSerialPort.writes, [bytes([0xFB, 13])])
            self.assertEqual(len(callbacks), 1)
            callbacks.pop(0)()

        self.assertEqual(worker.MCUSerialPort.writes, [bytes([0xFB, 13]), bytes([0xF3, 3])])
        self.assertEqual(len(callbacks), 1)

    def test_release_sort_long_release_uses_continuous_until_release_sort_preempts(self):
        worker = MCUTriggerWorker()
        worker.MCUSerialPort = _FakeSerialPort()
        worker.releaseTime = 250
        worker.releaseSortTime = 10
        worker.send_data_releaseSort = MCUTriggerWorker._mcu_frame(MCU_CMD_RELEASE_SORT, 10)

        callbacks = []

        def _capture_timer(_delay_ms, _timer_type, callback):
            callbacks.append(callback)

        with mock.patch("control_wangbo.MCUTriggerThread.QTimer.singleShot", side_effect=_capture_timer):
            worker.slot_btn_triggerReleaseSort()
            self.assertEqual(worker.MCUSerialPort.writes, [bytes([0xFB, 0xFF])])
            callbacks.pop(0)()

        self.assertEqual(worker.MCUSerialPort.writes, [bytes([0xFB, 0xFF]), bytes([0xF3, 10])])
        self.assertEqual(len(callbacks), 1)


if __name__ == "__main__":
    unittest.main()
