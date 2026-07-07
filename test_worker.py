import unittest
from unittest.mock import patch, MagicMock, mock_open
import os
import sys
import json
import shutil
from pathlib import Path
import tempfile
import requests

# Import the print worker module
import worker

# Set up temporary state file for tests
TEMP_STATE_FILE = Path(tempfile.gettempdir()) / "test_worker_state.json"
worker.STATE_FILE = TEMP_STATE_FILE


class TestStateManagement(unittest.TestCase):
    
    def setUp(self):
        # Reset local state before each test
        if TEMP_STATE_FILE.exists():
            TEMP_STATE_FILE.unlink()
        worker.PRINTER_KEY = "testkey"

    def tearDown(self):
        # Clean up temporary state file
        if TEMP_STATE_FILE.exists():
            TEMP_STATE_FILE.unlink()

    def test_make_safe_filename(self):
        self.assertEqual(worker.make_safe_filename("photo.jpg"), "photo.jpg")
        self.assertEqual(worker.make_safe_filename("photo$123.jpg"), "photo_123.jpg")
        self.assertEqual(worker.make_safe_filename("../../etc/passwd"), "passwd")
        self.assertEqual(worker.make_safe_filename(""), "downloaded_image.jpg")
        self.assertEqual(worker.make_safe_filename("."), "downloaded_image.jpg")
        self.assertEqual(worker.make_safe_filename(".."), "downloaded_image.jpg")

    def test_load_state_fresh(self):
        # State file doesn't exist yet
        state = worker.load_state()
        self.assertIsNone(state["currently_processing_job_id"])
        self.assertEqual(state["completed_job_ids"], [])
        self.assertIsNone(state["last_status_sent"])

    def test_load_state_corrupted(self):
        # Write corrupted JSON
        with open(TEMP_STATE_FILE, "w") as f:
            f.write("invalid json{")
        
        state = worker.load_state()
        self.assertIsNone(state["currently_processing_job_id"])
        self.assertEqual(state["completed_job_ids"], [])

    def test_save_and_load_state(self):
        state = {
            "currently_processing_job_id": 42,
            "completed_job_ids": [1, 2, 3],
            "last_status_sent": "printing"
        }
        worker.save_state(state)
        
        loaded = worker.load_state()
        self.assertEqual(loaded["currently_processing_job_id"], 42)
        self.assertEqual(loaded["completed_job_ids"], [1, 2, 3])
        self.assertEqual(loaded["last_status_sent"], "printing")

    def test_save_state_unbounded_completed_ids(self):
        # Create state with 120 completed jobs
        completed = list(range(120))
        state = {
            "currently_processing_job_id": None,
            "completed_job_ids": completed,
            "last_status_sent": None
        }
        worker.save_state(state)
        
        loaded = worker.load_state()
        # Should be capped at 100
        self.assertEqual(len(loaded["completed_job_ids"]), 100)
        # Should contain the most recent 100 (20 to 119)
        self.assertEqual(loaded["completed_job_ids"], completed[20:])

    def test_get_job_temp_dir(self):
        temp_dir = worker.get_job_temp_dir(123)
        self.assertTrue(temp_dir.name == "123")
        self.assertTrue("print-worker" in str(temp_dir))

    def test_cleanup_temp_dir(self):
        temp_dir = worker.get_job_temp_dir(999)
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / "test_photo.jpg"
        temp_file.write_text("dummy photo data")
        
        self.assertTrue(temp_file.exists())
        worker.cleanup_temp_dir(999)
        self.assertFalse(temp_file.exists())
        self.assertFalse(temp_dir.exists())


class TestBackendAPI(unittest.TestCase):
    
    def setUp(self):
        self.api = worker.BackendAPI(
            base_url="https://example.com/api",
            printer_key="secret-key",
            dry_run=False,
            send_status_in_dry_run=False
        )

    @patch("requests.get")
    def test_poll_next_job_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 101, "photos": []}
        mock_get.return_value = mock_response

        job = self.api.poll_next_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], 101)
        mock_get.assert_called_once_with(
            "https://example.com/api/printer/jobs/next",
            headers={"X-Printer-Key": "secret-key"},
            timeout=30
        )

    @patch("requests.get")
    def test_poll_next_job_no_content(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_get.return_value = mock_response

        job = self.api.poll_next_job()
        self.assertIsNone(job)

    @patch("requests.get")
    def test_poll_next_job_error_status(self, mock_get):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_get.return_value = mock_response

        job = self.api.poll_next_job()
        self.assertIsNone(job)

    @patch("requests.get")
    def test_poll_next_job_exception(self, mock_get):
        mock_get.side_effect = requests.RequestException("Network timeout")
        job = self.api.poll_next_job()
        self.assertIsNone(job)

    @patch("requests.post")
    def test_update_job_status_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        success = self.api.update_job_status(101, "completed")
        self.assertTrue(success)
        mock_post.assert_called_once_with(
            "https://example.com/api/printer/jobs/101/status",
            headers={"X-Printer-Key": "secret-key"},
            json={"status": "completed"},
            timeout=30
        )

    @patch("time.sleep")
    @patch("requests.post")
    def test_update_job_status_retry_and_success(self, mock_post, mock_sleep):
        mock_response_fail = MagicMock()
        mock_response_fail.raise_for_status.side_effect = requests.RequestException("Temporary failure")
        
        mock_response_ok = MagicMock()
        mock_response_ok.raise_for_status = MagicMock()
        
        mock_post.side_effect = [mock_response_fail, mock_response_ok]

        success = self.api.update_job_status(101, "printing")
        self.assertTrue(success)
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(2)

    @patch("time.sleep")
    @patch("requests.post")
    def test_update_job_status_all_attempts_fail(self, mock_post, mock_sleep):
        mock_post.side_effect = requests.RequestException("Connection refused")

        success = self.api.update_job_status(101, "failed", error_message="Printer jam")
        self.assertFalse(success)
        self.assertEqual(mock_post.call_count, 2)

    def test_update_job_status_dry_run_no_send(self):
        dry_api = worker.BackendAPI("http://dummy", "key", dry_run=True, send_status_in_dry_run=False)
        # Should succeed immediately without making a web request
        with patch("requests.post") as mock_post:
            success = dry_api.update_job_status(101, "completed")
            self.assertTrue(success)
            mock_post.assert_not_called()

    @patch("requests.post")
    def test_update_job_status_dry_run_with_send(self, mock_post):
        dry_api = worker.BackendAPI("https://dummy", "key", dry_run=True, send_status_in_dry_run=True)
        mock_response = MagicMock()
        mock_post.return_value = mock_response
        
        success = dry_api.update_job_status(101, "completed")
        self.assertTrue(success)
        mock_post.assert_called_once()

    @patch("requests.get")
    def test_download_photo_success_absolute_url(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.iter_content.return_value = [b"data_chunk_1", b"data_chunk_2"]
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmp_dir:
            dest_path = Path(tmp_dir) / "downloaded.jpg"
            success = self.api.download_photo("https://images.com/photo.jpg", dest_path)
            
            self.assertTrue(success)
            self.assertTrue(dest_path.exists())
            self.assertEqual(dest_path.read_bytes(), b"data_chunk_1data_chunk_2")

    @patch("requests.get")
    def test_download_photo_relative_url(self, mock_get):
        mock_response = MagicMock()
        mock_response.iter_content.return_value = [b"data"]
        mock_get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmp_dir:
            dest_path = Path(tmp_dir) / "downloaded.jpg"
            
            # Test simple host-relative path
            success = self.api.download_photo("/static/photo.jpg", dest_path)
            self.assertTrue(success)
            mock_get.assert_any_call(
                "https://example.com/static/photo.jpg",
                headers={"X-Printer-Key": "secret-key"},
                stream=True,
                timeout=60
            )
            
            # Test host-relative path starting with /api/ (as returned by production backend)
            success = self.api.download_photo("/api/photos/3/download", dest_path)
            self.assertTrue(success)
            mock_get.assert_any_call(
                "https://example.com/api/photos/3/download",
                headers={"X-Printer-Key": "secret-key"},
                stream=True,
                timeout=60
            )

    @patch("time.sleep")
    @patch("requests.get")
    def test_download_photo_retry_and_cleanup(self, mock_get, mock_sleep):
        mock_get.side_effect = requests.RequestException("Network down")

        with tempfile.TemporaryDirectory() as tmp_dir:
            dest_path = Path(tmp_dir) / "photo.jpg"
            
            success = self.api.download_photo("https://images.com/photo.jpg", dest_path)
            self.assertFalse(success)
            self.assertFalse(dest_path.exists())
            self.assertEqual(mock_get.call_count, 3)
            self.assertEqual(mock_sleep.call_count, 2)


class TestCupsIntegration(unittest.TestCase):

    @patch("subprocess.run")
    def test_check_cups_printer_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "printer SELPHY is idle."
        mock_run.return_value = mock_proc

        result = worker.check_cups_printer("SELPHY")
        self.assertTrue(result)
        mock_run.assert_called_once_with(
            ["lpstat", "-p", "SELPHY"],
            stdout=subprocess_pipe(),
            stderr=subprocess_pipe(),
            text=True
        )

    @patch("subprocess.run")
    def test_check_cups_printer_failed(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "printer SELPHY not found."
        mock_run.return_value = mock_proc

        result = worker.check_cups_printer("SELPHY")
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_check_cups_printer_not_found_utility(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        
        # When lpstat is missing, function returns True (graceful fallback)
        result = worker.check_cups_printer("SELPHY")
        self.assertTrue(result)

    @patch("subprocess.run")
    def test_print_file_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "request id is SELPHY-42"
        mock_run.return_value = mock_proc

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "photo.jpg"
            file_path.write_text("photo data")
            
            result = worker.print_file("SELPHY", file_path, dry_run=False)
            self.assertTrue(result)
            mock_run.assert_called_once_with(
                ["lp", "-d", "SELPHY", str(file_path)],
                stdout=subprocess_pipe(),
                stderr=subprocess_pipe(),
                text=True
            )

    @patch("subprocess.run")
    def test_print_file_failed(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stderr = "Error: printer not found"
        mock_run.return_value = mock_proc

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "photo.jpg"
            result = worker.print_file("SELPHY", file_path, dry_run=False)
            self.assertFalse(result)

    @patch("subprocess.run")
    def test_print_file_missing_lp(self, mock_run):
        mock_run.side_effect = FileNotFoundError()

        with tempfile.TemporaryDirectory() as tmp_dir:
            file_path = Path(tmp_dir) / "photo.jpg"
            result = worker.print_file("SELPHY", file_path, dry_run=False)
            self.assertFalse(result)

    def test_print_file_dry_run(self):
        # In dry run mode, subprocess.run is not called and it returns True
        with patch("subprocess.run") as mock_run:
            result = worker.print_file("SELPHY", Path("/dummy/path"), dry_run=True)
            self.assertTrue(result)
            mock_run.assert_not_called()

    def test_wait_for_printer_idle_dry_run(self):
        with patch("subprocess.run") as mock_run:
            result = worker.wait_for_printer_idle("SELPHY", dry_run=True)
            self.assertTrue(result)
            mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_wait_for_printer_idle_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "printer SELPHY is idle."
        mock_run.return_value = mock_proc

        result = worker.wait_for_printer_idle("SELPHY", dry_run=False)
        self.assertTrue(result)
        mock_run.assert_called_once()

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_wait_for_printer_idle_loop(self, mock_run, mock_sleep):
        mock_proc_busy = MagicMock()
        mock_proc_busy.returncode = 0
        mock_proc_busy.stdout = "printer SELPHY now printing SELPHY-4..."

        mock_proc_idle = MagicMock()
        mock_proc_idle.returncode = 0
        mock_proc_idle.stdout = "printer SELPHY is idle."

        mock_run.side_effect = [mock_proc_busy, mock_proc_idle]

        result = worker.wait_for_printer_idle("SELPHY", dry_run=False, check_interval_seconds=1)
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 2)
        mock_sleep.assert_called_once_with(1)


class TestJobProcessing(unittest.TestCase):

    def setUp(self):
        if TEMP_STATE_FILE.exists():
            TEMP_STATE_FILE.unlink()
        self.api = MagicMock(spec=worker.BackendAPI)
        self.api.dry_run = False

    def tearDown(self):
        if TEMP_STATE_FILE.exists():
            TEMP_STATE_FILE.unlink()

    @patch("worker.wait_for_printer_idle")
    @patch("worker.print_file")
    def test_process_single_job_success(self, mock_print, mock_wait):
        self.api.download_photo.return_value = True
        self.api.update_job_status.return_value = True
        mock_print.return_value = True

        job = {
            "id": 202,
            "photos": [
                {"original_filename": "cat.jpg", "download_url": "/cat.jpg"},
                {"original_filename": "dog.jpg", "download_url": "/dog.jpg"}
            ]
        }

        # Run process
        result = worker.process_single_job(job, self.api)
        self.assertTrue(result)

        # Check API status calls
        self.api.update_job_status.assert_any_call(202, "printing")
        self.api.update_job_status.assert_any_call(202, "completed")

        # Check printer was called twice
        self.assertEqual(mock_print.call_count, 2)

        # Temp directory should be cleaned up
        temp_dir = worker.get_job_temp_dir(202)
        self.assertFalse(temp_dir.exists())

        # State should contain completed ID
        state = worker.load_state()
        self.assertEqual(state["completed_job_ids"], [202])
        self.assertIsNone(state["currently_processing_job_id"])

    @patch("worker.print_file")
    def test_process_single_job_download_failure(self, mock_print):
        # First download succeeds, second fails
        self.api.download_photo.side_effect = [True, False]
        self.api.update_job_status.return_value = True

        job = {
            "id": 203,
            "photos": [
                {"original_filename": "cat.jpg", "download_url": "/cat.jpg"},
                {"original_filename": "dog.jpg", "download_url": "/dog.jpg"}
            ]
        }

        result = worker.process_single_job(job, self.api)
        self.assertFalse(result)

        # Never reached printing state
        self.api.update_job_status.assert_any_call(203, "failed", error_message="Failed to download photo: dog.jpg")
        mock_print.assert_not_called()

        # State updated to failed/seen to prevent loop
        state = worker.load_state()
        self.assertEqual(state["completed_job_ids"], [203])

    @patch("worker.print_file")
    def test_process_single_job_print_failure(self, mock_print):
        self.api.download_photo.return_value = True
        self.api.update_job_status.return_value = True
        # Print fails
        mock_print.return_value = False

        job = {
            "id": 204,
            "photos": [
                {"original_filename": "cat.jpg", "download_url": "/cat.jpg"}
            ]
        }

        result = worker.process_single_job(job, self.api)
        self.assertFalse(result)

        # Checked update to failed with info
        self.api.update_job_status.assert_any_call(204, "failed", error_message="CUPS print command failed for photo cat.jpg")

    def test_process_single_job_already_processed(self):
        # Prepopulate state
        state = worker.load_state()
        state["completed_job_ids"].append(205)
        worker.save_state(state)

        job = {"id": 205, "photos": []}
        result = worker.process_single_job(job, self.api)
        
        # Should return True instantly and skip processing
        self.assertTrue(result)
        self.api.download_photo.assert_not_called()


class TestRecoveryLogic(unittest.TestCase):

    def setUp(self):
        if TEMP_STATE_FILE.exists():
            TEMP_STATE_FILE.unlink()
        self.api = MagicMock(spec=worker.BackendAPI)

    def tearDown(self):
        if TEMP_STATE_FILE.exists():
            TEMP_STATE_FILE.unlink()

    def test_handle_startup_recovery_no_interrupted_job(self):
        # Fresh state
        worker.handle_startup_recovery(self.api)
        self.api.update_job_status.assert_not_called()

    def test_handle_startup_recovery_with_interrupted_job(self):
        # Set up interrupted job state
        state = worker.load_state()
        state["currently_processing_job_id"] = 301
        state["last_status_sent"] = "printing"
        worker.save_state(state)

        # Create temporary folders/files to check cleanup
        temp_dir = worker.get_job_temp_dir(301)
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_file = temp_dir / "unfinished.jpg"
        temp_file.write_text("unfinished print data")

        self.api.update_job_status.return_value = True

        # Run recovery
        worker.handle_startup_recovery(self.api)

        # Verify update status called
        self.api.update_job_status.assert_called_once_with(
            301, "failed", error_message="Job interrupted due to worker crash/reboot"
        )

        # Verify cleanup occurred
        self.assertFalse(temp_file.exists())
        self.assertFalse(temp_dir.exists())

        # Verify state is cleared and moved to completed
        new_state = worker.load_state()
        self.assertIsNone(new_state["currently_processing_job_id"])
        self.assertEqual(new_state["completed_job_ids"], [301])


class TestMainWorkerLoop(unittest.TestCase):

    @patch("sys.exit")
    def test_main_missing_printer_key(self, mock_exit):
        # PRINTER_KEY is None or default
        worker.PRINTER_KEY = None
        with patch("argparse.ArgumentParser.parse_args") as mock_args:
            mock_args.return_value = MagicMock(dry_run=False)
            worker.main()
            mock_exit.assert_called_once_with(1)

    @patch("sys.exit")
    @patch("worker.check_cups_printer")
    def test_main_missing_printer_in_cups_exits(self, mock_check, mock_exit):
        worker.PRINTER_KEY = "testkey"
        mock_check.return_value = False

        with patch("argparse.ArgumentParser.parse_args") as mock_args:
            mock_args.return_value = MagicMock(dry_run=False)
            worker.main()
            mock_exit.assert_called_once_with(1)

    @patch("worker.check_cups_printer")
    @patch("worker.BackendAPI")
    @patch("worker.handle_startup_recovery")
    @patch("time.sleep")
    def test_main_once_loop(self, mock_sleep, mock_recovery, mock_api_class, mock_check):
        worker.PRINTER_KEY = "testkey"
        mock_check.return_value = True

        # Mock API client instance
        mock_api = MagicMock()
        mock_api.poll_next_job.return_value = None
        mock_api_class.return_value = mock_api

        with patch("argparse.ArgumentParser.parse_args") as mock_args:
            mock_args.return_value = MagicMock(once=True, dry_run=True, send_status_in_dry_run=False)
            
            worker.main()

            # Ensure recovery and poll checks occurred
            mock_recovery.assert_called_once_with(mock_api)
            mock_api.poll_next_job.assert_called_once()
            # Since once=True, sleep should not be called
            mock_sleep.assert_not_called()


# Helper function to get subprocess PIPE object
def subprocess_pipe():
    return -1  # Equivalent to subprocess.PIPE
    

if __name__ == "__main__":
    unittest.main()
