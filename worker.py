#!/usr/bin/env python3
"""
Raspberry Pi Print Worker for CUPS
Polls a remote backend for print jobs, downloads images, and prints them via CUPS.
"""

import os
import sys
import time
import json
import re
import logging
import argparse
import subprocess
import urllib.parse
from pathlib import Path
import tempfile
import requests
from dotenv import load_dotenv

# Set up logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("print-worker")

# Load environment variables
load_dotenv()

# Configurations
API_BASE_URL = os.getenv("API_BASE_URL", "https://frontend-nexthouseinstant.pages.dev/api").rstrip("/")
PRINTER_KEY = os.getenv("PRINTER_KEY")
PRINTER_NAME = os.getenv("PRINTER_NAME", "SELPHY")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))

# Local state file path
STATE_FILE = Path(__file__).resolve().parent / ".worker_state.json"

# Safe filename pattern
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


class BackendAPI:
    """Manages all HTTP requests to the backend server."""
    
    def __init__(self, base_url: str, printer_key: str, dry_run: bool, send_status_in_dry_run: bool):
        self.base_url = base_url
        self.printer_key = printer_key
        self.dry_run = dry_run
        self.send_status_in_dry_run = send_status_in_dry_run
        self.headers = {
            "X-Printer-Key": self.printer_key
        }

    def poll_next_job(self) -> dict | None:
        """Polls the API for the next job."""
        url = f"{self.base_url}/printer/jobs/next"
        try:
            logger.debug(f"Polling endpoint: {url}")
            response = requests.get(url, headers=self.headers, timeout=30)
            if response.status_code == 204:
                return None
            elif response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Unexpected status code while polling: {response.status_code}. Response: {response.text}")
                return None
        except requests.RequestException as e:
            logger.error(f"Connection error while polling: {e}")
            return None

    def update_job_status(self, job_id: int, status: str, error_message: str | None = None) -> bool:
        """Notifies the backend of a job status change."""
        if self.dry_run and not self.send_status_in_dry_run:
            logger.info(f"[DRY-RUN] Skipped updating backend status for job {job_id} to '{status}'.")
            return True

        url = f"{self.base_url}/printer/jobs/{job_id}/status"
        payload = {"status": status}
        if error_message:
            payload["error_message"] = error_message

        logger.info(f"Updating job {job_id} status to '{status}' on backend...")
        for attempt in range(1, 3):
            try:
                response = requests.post(url, headers=self.headers, json=payload, timeout=30)
                response.raise_for_status()
                logger.info(f"Successfully updated status for job {job_id} to '{status}'.")
                return True
            except requests.RequestException as e:
                logger.warning(f"Attempt {attempt}/2 failed to update status for job {job_id} to '{status}': {e}")
                if attempt < 2:
                    time.sleep(2)
        logger.error(f"Failed to update status for job {job_id} after 2 attempts.")
        return False

    def download_photo(self, download_url: str, dest_path: Path) -> bool:
        """Downloads a photo and saves it to the local path, retrying on failure."""
        if download_url.startswith("http://") or download_url.startswith("https://"):
            full_url = download_url
        else:
            parsed_base = urllib.parse.urlparse(self.base_url)
            host_root = f"{parsed_base.scheme}://{parsed_base.netloc}"
            full_url = urllib.parse.urljoin(host_root, download_url)

        logger.info(f"Downloading photo from {full_url}")
        for attempt in range(1, 4):
            try:
                response = requests.get(full_url, headers=self.headers, stream=True, timeout=60)
                response.raise_for_status()

                # Ensure destination folder exists
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                
                with open(dest_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                logger.info(f"Download complete: {dest_path.name}")
                return True
            except requests.RequestException as e:
                logger.warning(f"Attempt {attempt}/3 failed to download photo: {e}")
                if dest_path.exists():
                    try:
                        dest_path.unlink()
                    except OSError:
                        pass
                if attempt < 3:
                    time.sleep(2)
        logger.error(f"Failed to download photo after 3 attempts: {download_url}")
        return False


# State management functions
def load_state() -> dict:
    """Loads the local print worker state from JSON."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                if not isinstance(state.get("completed_job_ids"), list):
                    state["completed_job_ids"] = []
                return state
        except Exception as e:
            logger.warning(f"Could not read state file, starting fresh: {e}")
    return {
        "currently_processing_job_id": None,
        "completed_job_ids": [],
        "last_status_sent": None
    }


def save_state(state: dict):
    """Saves the local print worker state to JSON."""
    try:
        # Prevent unbounded growth of completed list (keep last 100 jobs)
        if len(state.get("completed_job_ids", [])) > 100:
            state["completed_job_ids"] = state["completed_job_ids"][-100:]
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving state to file: {e}")


def make_safe_filename(name: str) -> str:
    """Sanitizes file name to prevent security vulnerabilities."""
    base = os.path.basename(name)
    safe = SAFE_FILENAME_RE.sub("_", base)
    if not safe or safe in (".", ".."):
        safe = "downloaded_image.jpg"
    return safe


def get_job_temp_dir(job_id: int) -> Path:
    """Gets the path of the temporary folder for a specific job."""
    base_dir = Path("/tmp") if os.name != "nt" else Path(tempfile.gettempdir())
    return base_dir / "print-worker" / str(job_id)


def cleanup_temp_dir(job_id: int):
    """Deletes temporary folder and files created for a job."""
    temp_dir = get_job_temp_dir(job_id)
    if temp_dir.exists():
        try:
            logger.info(f"Cleaning up temporary files in {temp_dir}")
            for item in temp_dir.iterdir():
                if item.is_file():
                    item.unlink()
            temp_dir.rmdir()
            logger.info(f"Temporary folder cleaned up successfully: {temp_dir}")
        except Exception as e:
            logger.warning(f"Error deleting temp files in {temp_dir}: {e}")


# CUPS integration
def check_cups_printer(printer_name: str) -> bool:
    """Verifies that the printer is correctly registered in CUPS, attempting to enable it if disabled."""
    try:
        result = subprocess.run(
            ["lpstat", "-p", printer_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode == 0:
            output = result.stdout
            logger.info(f"CUPS printer verification success: {output.strip()}")
            if "disabled" in output.lower():
                logger.warning(f"Printer '{printer_name}' is disabled. Attempting to enable it...")
                enable_result = subprocess.run(
                    ["cupsenable", printer_name],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                if enable_result.returncode == 0:
                    logger.info(f"Successfully enabled printer '{printer_name}'.")
                else:
                    logger.warning(
                        f"Failed to enable printer '{printer_name}' via cupsenable: {enable_result.stderr.strip()}. "
                        "The printer will remain disabled until resolved manually."
                    )
            return True
        else:
            logger.error(f"CUPS printer verification failed: {result.stderr.strip()}")
            return False
    except FileNotFoundError:
        logger.warning("lpstat utility not found. CUPS printer check skipped (expected on non-Linux OS).")
        return True
    except Exception as e:
        logger.error(f"Error checking CUPS status: {e}")
        return False


def wait_for_printer_idle(printer_name: str, dry_run: bool, check_interval_seconds: int = 5, max_wait_seconds: int = 180) -> bool:
    """Blocks until the CUPS printer is idle, checking at regular intervals."""
    if dry_run:
        return True

    logger.info(f"Waiting for printer '{printer_name}' to become idle...")
    start_time = time.time()
    communication_warning_count = 0
    
    while time.time() - start_time < max_wait_seconds:
        try:
            result = subprocess.run(
                ["lpstat", "-p", printer_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                output = result.stdout.lower()
                if "is idle" in output:
                    logger.info(f"Printer '{printer_name}' is idle. Proceeding.")
                    return True
                
                # Check for disabled status and try to enable it
                if "disabled" in output:
                    logger.warning(f"Printer '{printer_name}' is disabled. Attempting to enable it...")
                    subprocess.run(["cupsenable", printer_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                
                # Check for communication warnings (offline, unplugged, sleep, etc.)
                if "waiting for printer to become available" in output or "waiting for device" in output:
                    communication_warning_count += 1
                    logger.warning(
                        f"Printer '{printer_name}' is waiting for device (attempt {communication_warning_count}/3). "
                        "Attempting to resume it via cupsenable..."
                    )
                    subprocess.run(["cupsenable", printer_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    
                    if communication_warning_count >= 3:
                        logger.warning(
                            f"Printer '{printer_name}' remains stuck in communication warning state. "
                            "Proceeding to prevent blocking the event loop."
                        )
                        return True
                else:
                    # Reset communication warning count if status is other non-idle (e.g. printing normally)
                    communication_warning_count = 0
                    logger.debug(f"Printer '{printer_name}' status: {result.stdout.strip()}")
            else:
                logger.warning(f"lpstat returned exit code {result.returncode}: {result.stderr.strip()}")
                return True
        except FileNotFoundError:
            logger.warning("lpstat utility not found. Skipping printer idle check.")
            return True
        except Exception as e:
            logger.warning(f"Error checking printer status: {e}")
            return True
            
        time.sleep(check_interval_seconds)
        
    logger.warning(f"Printer '{printer_name}' did not become idle within {max_wait_seconds} seconds. Continuing anyway.")
    return False


def print_file(printer_name: str, file_path: Path, dry_run: bool) -> bool:
    """Sends a local file to the CUPS printer queue."""
    if dry_run:
        logger.info(f"[DRY-RUN] lp -d {printer_name} {file_path}")
        return True

    logger.info(f"Sending file to CUPS printer '{printer_name}': {file_path}")
    try:
        result = subprocess.run(
            ["lp", "-d", printer_name, str(file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        if result.returncode == 0:
            logger.info(f"Print job successfully queued. Output: {result.stdout.strip()}")
            return True
        else:
            logger.error(f"CUPS lp command returned error: {result.stderr.strip()}")
            return False
    except FileNotFoundError:
        logger.error("lp utility not found. Make sure CUPS is installed and in path.")
        return False
    except Exception as e:
        logger.error(f"Unexpected exception while printing: {e}")
        return False


# Recovery Logic
def handle_startup_recovery(api: BackendAPI):
    """Checks for interrupted jobs on startup and cleans them up."""
    state = load_state()
    job_id = state.get("currently_processing_job_id")
    if job_id is not None:
        logger.warning(f"Recovery active: Interrupted job ID {job_id} detected. Reporting failure.")
        
        # Mark as failed
        err_msg = "Job interrupted due to worker crash/reboot"
        success = api.update_job_status(job_id, "failed", error_message=err_msg)
        if success:
            logger.info(f"Job {job_id} marked as failed on backend successfully.")
        else:
            logger.warning(f"Could not report failure status to backend for job {job_id}.")

        # Cleanup files
        cleanup_temp_dir(job_id)

        # Move to completed/processed list to prevent duplicate attempts
        if job_id not in state["completed_job_ids"]:
            state["completed_job_ids"].append(job_id)

        # Clear active variables
        state["currently_processing_job_id"] = None
        state["last_status_sent"] = None
        save_state(state)
        logger.info("Startup crash recovery finished.")


def process_single_job(job: dict, api: BackendAPI) -> bool:
    """Core logic to process, download, print and report status of a single job."""
    job_id = job.get("id")
    photos = job.get("photos", [])

    if job_id is None:
        logger.error("Job JSON structure missing 'id' key.")
        return False

    state = load_state()

    # Skip seen jobs
    if job_id in state.get("completed_job_ids", []):
        logger.warning(f"Skipping job {job_id} because it was already processed locally.")
        return True

    logger.info(f"Starting processing of job {job_id} ({len(photos)} photos)")
    
    # 1. Update State: set job as currently active
    state["currently_processing_job_id"] = job_id
    state["last_status_sent"] = None
    save_state(state)

    temp_dir = get_job_temp_dir(job_id)
    downloaded_paths = []
    success = True
    error_msg = None

    # 2. Download all files
    for idx, photo in enumerate(photos):
        original_name = photo.get("original_filename", f"photo_{idx}.jpg")
        safe_name = make_safe_filename(original_name)
        dest_path = temp_dir / safe_name
        download_url = photo.get("download_url")

        if not download_url:
            success = False
            error_msg = f"Photo metadata missing download_url for file {original_name}"
            logger.error(error_msg)
            break

        download_success = api.download_photo(download_url, dest_path)
        if not download_success:
            success = False
            error_msg = f"Failed to download photo: {original_name}"
            break
        downloaded_paths.append(dest_path)

    # 3. Print if download succeeded
    if success:
        # A. Notify backend of 'printing' state exactly once
        status_ok = api.update_job_status(job_id, "printing")
        if status_ok:
            state["last_status_sent"] = "printing"
            save_state(state)

        # B. Print all photos
        for idx, file_path in enumerate(downloaded_paths):
            if idx > 0:
                # Wait dynamically until printer is idle
                wait_for_printer_idle(PRINTER_NAME, api.dry_run)

            print_ok = print_file(PRINTER_NAME, file_path, api.dry_run)
            if not print_ok:
                success = False
                error_msg = f"CUPS print command failed for photo {file_path.name}"
                logger.error(error_msg)
                break
            
            # Give CUPS 2 seconds to transition states before checking next
            if not api.dry_run:
                time.sleep(2)

    # 4. Final status report and state update
    if success:
        logger.info(f"Successfully processed all parts of job {job_id}.")
        # Update backend status
        api.update_job_status(job_id, "completed")
        
        # Save state
        state["completed_job_ids"].append(job_id)
        state["currently_processing_job_id"] = None
        state["last_status_sent"] = None
        save_state(state)
        
        # Cleanup
        cleanup_temp_dir(job_id)
        return True
    else:
        logger.error(f"Job {job_id} failed: {error_msg}")
        # Update backend status
        api.update_job_status(job_id, "failed", error_message=error_msg)
        
        # Save state (mark as seen to avoid reprint loops)
        state["completed_job_ids"].append(job_id)
        state["currently_processing_job_id"] = None
        state["last_status_sent"] = None
        save_state(state)

        # Cleanup
        cleanup_temp_dir(job_id)
        return False


def main():
    parser = argparse.ArgumentParser(description="Raspberry Pi CUPS Print Worker")
    parser.add_argument("--once", action="store_true", help="Run polling once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Simulate print commands instead of calling lp")
    parser.add_argument("--send-status-in-dry-run", action="store_true", help="Send HTTP status updates in dry-run mode")
    args = parser.parse_args()

    logger.info("Initializing Print Worker...")
    if args.dry_run:
        logger.info("Running in DRY-RUN mode (printing commands will be simulated).")
        if args.send_status_in_dry_run:
            logger.info("Status updates will be sent to the backend despite dry-run.")
        else:
            logger.info("Status updates to the backend will be skipped.")

    if not PRINTER_KEY or PRINTER_KEY == "changeme":
        logger.error("PRINTER_KEY environment variable is missing or set to default 'changeme'. Please check your .env file.")
        sys.exit(1)

    # Startup checks
    if not check_cups_printer(PRINTER_NAME):
        if args.dry_run:
            logger.warning(f"Printer '{PRINTER_NAME}' not found in CUPS. Continuing anyway since --dry-run is active.")
        else:
            logger.critical(f"Printer '{PRINTER_NAME}' must be registered in CUPS before starting worker. Exiting.")
            sys.exit(1)

    # Initialize client
    api = BackendAPI(
        base_url=API_BASE_URL,
        printer_key=PRINTER_KEY,
        dry_run=args.dry_run,
        send_status_in_dry_run=args.send_status_in_dry_run
    )

    # Handle startup crash recovery
    handle_startup_recovery(api)

    logger.info("Starting worker event loop...")
    while True:
        logger.debug("Checking for next print job...")
        job = api.poll_next_job()
        
        if job:
            process_single_job(job, api)
        else:
            logger.debug("No jobs available.")

        if args.once:
            logger.info("Terminating because --once flag was specified.")
            break

        # Sleep before next poll
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Worker process interrupted by user. Exiting.")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Fatal unhandled exception in worker main execution: {e}", exc_info=True)
        sys.exit(1)
