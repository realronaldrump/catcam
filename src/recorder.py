import time
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path
from config import Config

def wait_for_box():
    """Waits for the Box mount to be available and writable."""
    print("Waiting for Box mount at /data/box...")
    max_attempts = 60
    for i in range(max_attempts):
        if Config.BOX_ROOT.exists() and os.access(Config.BOX_ROOT, os.W_OK):
            print("Box mount is ready!")
            return True
        print(f"Waiting... ({i+1}/{max_attempts})")
        time.sleep(2)
    return False

def ensure_directories():
    """Ensures the target directory for today exists."""
    conf = Config.load()
    full_path = Config.BOX_ROOT / conf["SUBFOLDER"]
    today_path = full_path / datetime.now().strftime("%Y/%m/%d")
    today_path.mkdir(parents=True, exist_ok=True)
    return today_path, conf

def record_stream():
    """Starts the FFmpeg recording process."""
    if not wait_for_box():
        print("ERROR: Box mount failed or timed out.")
        sys.exit(1)

    while True:
        try:
            # Load config fresh each loop
            today_path, conf = ensure_directories()
            
            full_path = Config.BOX_ROOT / conf["SUBFOLDER"]
            file_template = str(full_path) + "/%Y/%m/%d/%p-%I-%M-%S.mp4"
            
            rtsp_url = f"rtsp://{conf['CAMERA_USER']}:{conf['CAMERA_PASS']}@{conf['CAMERA_IP']}:554/h264Preview_01_main"
            segment_time = conf["SEGMENT_TIME"]

            print(f"Starting recording from {conf['CAMERA_IP']}...")
            print(f"Segment time: {segment_time}s")
            
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-rtsp_transport", "tcp",
                "-i", rtsp_url,
                "-c", "copy",
                "-map", "0",
                "-f", "segment",
                "-segment_time", str(segment_time),
                "-strftime", "1",
                file_template
            ]

            # Run FFmpeg
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            print(f"FFmpeg started with PID {process.pid}")
            process.wait()
            
            print("FFmpeg exited. Restarting in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            print(f"Error running FFmpeg: {e}")
            time.sleep(10)

if __name__ == "__main__":
    # Flush stdout for Docker logs
    sys.stdout.reconfigure(line_buffering=True)
    record_stream()
