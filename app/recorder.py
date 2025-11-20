import os
import time
import subprocess
import datetime
import sys

# Configuration
# We use environment variables with defaults for the "hands-off" approach
CAMERA_USER = os.getenv("CAMERA_USER", "admin")
CAMERA_PASS = os.getenv("CAMERA_PASS", "CoonCam19")
CAMERA_IP = os.getenv("CAMERA_IP", "192.168.1.163")
SEGMENT_TIME = os.getenv("SEGMENT_TIME", "900")
SUBFOLDER = os.getenv("SUBFOLDER", "Other/CatCam")
BOX_ROOT = "/data/Box"  # Fixed path inside container

def wait_for_mount():
    """Waits for the Box volume to be available and writable."""
    print("Waiting for Box mount at /data/Box...")
    max_attempts = 60
    for attempt in range(max_attempts):
        if os.path.exists(BOX_ROOT) and os.access(BOX_ROOT, os.W_OK):
            print("Box mount is ready!")
            return True
        print(f"Wait... ({attempt + 1}/{max_attempts})")
        time.sleep(2)
    
    print("ERROR: Box mount failed.")
    return False

def ensure_directories():
    """Creates necessary directories for today's recordings."""
    full_path = os.path.join(BOX_ROOT, SUBFOLDER)
    today_str = datetime.datetime.now().strftime("%Y/%m/%d")
    today_path = os.path.join(full_path, today_str)
    
    os.makedirs(today_path, exist_ok=True)
    return full_path, today_path

def record_stream():
    """Starts the FFMPEG recording process."""
    if not wait_for_mount():
        sys.exit(1)

    full_path, today_path = ensure_directories()
    print(f"Recording to: {today_path}")
    print(f"Segment time: {SEGMENT_TIME} seconds")

    # RTSP URL
    rtsp_url = f"rtsp://{CAMERA_USER}:{CAMERA_PASS}@{CAMERA_IP}:554/h264Preview_01_main"
    
    # Output template: /data/Box/Other/CatCam/%Y/%m/%d/%p-%I-%M-%S.mp4
    # Note: We need to escape the % for python string formatting if we were using it, 
    # but here we pass it to ffmpeg.
    # However, ffmpeg strftime uses local time.
    
    # We want the file template to be passed to ffmpeg. 
    # ffmpeg -f segment -strftime 1 means the output filename is processed by strftime.
    # The directory structure %Y/%m/%d must exist? 
    # Actually ffmpeg might not create directories automatically with strftime.
    # The shell script created the directory `mkdir -p "$TODAY_PATH"`.
    # But since the script runs continuously, and days change, we might need a way to handle directory creation at midnight.
    # The original script just ran ffmpeg once. If it runs for days, ffmpeg might fail if the new day's dir doesn't exist.
    # BUT, the original script had: `FILE_TEMPLATE="$FULL_PATH/%Y/%m/%d/%p-%I-%M-%S.mp4"`
    # and `mkdir -p "$TODAY_PATH"`.
    # If ffmpeg runs across midnight, it will try to write to the new date dir. 
    # If that dir doesn't exist, ffmpeg will fail.
    # The original script didn't handle midnight directory creation explicitly in a loop, 
    # so it likely relied on restarting or just failing and restarting (systemd Restart=always).
    # We will rely on the same behavior: if ffmpeg fails, this script crashes, supervisord restarts it, 
    # and `ensure_directories` runs again.
    
    file_template = os.path.join(BOX_ROOT, SUBFOLDER, "%Y/%m/%d/%p-%I-%M-%S.mp4")

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", SEGMENT_TIME,
        "-strftime", "1",
        file_template
    ]

    print(f"Starting FFMPEG: {' '.join(cmd)}")
    
    # Run ffmpeg. If it crashes, we exit, and supervisord restarts us.
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"FFMPEG crashed with error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    # Basic retry loop just in case, though supervisord handles this too.
    while True:
        try:
            record_stream()
        except Exception as e:
            print(f"Recorder error: {e}")
            time.sleep(5)
