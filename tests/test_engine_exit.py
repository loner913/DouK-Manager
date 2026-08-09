from __future__ import annotations

import unittest

from douk_manager.core.engine import assess_process_exit


class EngineExitAssessmentTests(unittest.TestCase):
    def test_zero_exit_never_claims_download_success(self) -> None:
        result = assess_process_exit(0)

        self.assertTrue(result.normal_exit)
        self.assertEqual(result.log_status, "normal_exit_download_result_unverified")
        self.assertIn("进程正常退出", result.detail)
        self.assertIn("不代表所有账号均获取或下载成功", result.detail)

    def test_nonzero_exit_is_abnormal_and_stops_queue(self) -> None:
        result = assess_process_exit(3)

        self.assertFalse(result.normal_exit)
        self.assertEqual(result.log_status, "abnormal_exit")
        self.assertIn("退出码=3", result.headline)
        self.assertIn("剩余任务不会启动", result.detail)

    def test_unknown_exit_is_not_treated_as_success(self) -> None:
        result = assess_process_exit(None)

        self.assertFalse(result.normal_exit)
        self.assertEqual(result.log_status, "abnormal_exit")


if __name__ == "__main__":
    unittest.main()
