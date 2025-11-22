import time
import subprocess
import sys
import os
import threading
from datetime import datetime, timedelta
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

def generate_thumbnail(video_path: Path) -> bool:
    """
    Generates thumbnail from first frame of video.
    Returns True if thumbnail exists or was successfully created.
    Returns False if generation failed.
    """
    thumb_path = video_path.with_suffix('.thumb.jpg')
    
    # If it already exists, we consider it a success
    if thumb_path.exists():
        return True
    
    try:
        # Run ffmpeg to extract one frame
        subprocess.run([
            'ffmpeg', '-nostdin',
            '-i', str(video_path),
            '-vframes', '1',
            '-q:v', '2',
            '-y',
            str(thumb_path)
        ], capture_output=True, check=True, timeout=15)
        
        print(f"Generated thumbnail: {thumb_path.name}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg failed for {video_path.name}: {e.stderr.decode().strip()}")
        return False
    except Exception as e:
        print(f"Failed to generate thumbnail for {video_path.name}: {e}")
        return False

def thumbnail_watcher():
    """
    Background thread to watch for new videos and generate thumbnails.
    Fixes previous race condition by ensuring files are stable before processing.
    """
    seen_files = set()
    
    print("Thumbnail watcher started.")
    
    while True:
        try:
            conf = Config.load()
            today_path = Config.BOX_ROOT / conf["SUBFOLDER"] / datetime.now().strftime("%Y/%m/%d")
            
            if today_path.exists():
                # List all mp4 files
                files = list(today_path.glob("*.mp4"))
                
                for video in files:
                    # Skip if we've already successfully processed this file
                    if video in seen_files:
                        continue

                    # STABILITY CHECK:
                    # Skip files modified in the last 15 seconds.
                    # This ensures we don't try to read a file currently being written by the recorder.
                    try:
                        last_modified = video.stat().st_mtime
                        if (time.time() - last_modified) < 15:
                            continue
                    except FileNotFoundError:
                        # File might have been deleted during iteration
                        continue

                    # Attempt generation synchronously to avoid spawning too many threads
                    # and to ensure we know if it succeeded.
                    if generate_thumbnail(video):
                        seen_files.add(video)
            
            time.sleep(10)  # Check every 10 seconds
            
        except Exception as e:
            print(f"Thumbnail watcher error: {e}")
            time.sleep(30)

def get_seconds_until_midnight():
    """Calculates seconds remaining until the next midnight."""
    now = datetime.now()
    tomorrow = now + timedelta(days=1)
    midnight = datetime(year=tomorrow.year, month=tomorrow.month, day=tomorrow.day, hour=0, minute=0, second=0)
    return int((midnight - now).total_seconds())

def record_stream():
    """Starts the FFmpeg recording process."""
    if not wait_for_box():
        print("ERROR: Box mount failed or timed out.")
        sys.exit(1)

    # Start thumbnail generation watcher in a background thread
    thumb_thread = threading.Thread(target=thumbnail_watcher, daemon=True)
    thumb_thread.start()

    # Track last config mtime to detect changes
    last_config_mtime = 0
    if Config.SETTINGS_FILE.exists():
        last_config_mtime = Config.SETTINGS_FILE.stat().st_mtime

    while True:
        try:
            # Load config fresh each loop
            today_path, conf = ensure_directories()
            
            full_path = Config.BOX_ROOT / conf["SUBFOLDER"]
            file_template = str(full_path) + "/%Y/%m/%d/%p-%I-%M-%S.mp4"
            
            rtsp_url = f"rtsp://{conf['CAMERA_USER']}:{conf['CAMERA_PASS']}@{conf['CAMERA_IP']}:554/h264Preview_01_main"
            segment_time = conf["SEGMENT_TIME"]
            
            # Calculate duration to run until midnight
            duration = get_seconds_until_midnight()
            if duration < 10: duration = 10 

            print(f"Starting recording from {conf['CAMERA_IP']}...")
            print(f"Segment time: {segment_time}s")
            print(f"Running for {duration} seconds (until midnight)...")
            
            cmd = [
                "ffmpeg",
                "-nostdin",
                "-rtsp_transport", "tcp",
                "-timeout", "5000000",
                "-i", rtsp_url,
                "-c", "copy",
                "-map", "0",
                "-f", "segment",
                "-segment_time", str(segment_time),
                "-strftime", "1",
                "-t", str(duration),
                file_template
            ]

            # Run FFmpeg
            process = subprocess.Popen(cmd)
            print(f"FFmpeg started with PID {process.pid}")
            
            # Monitor process and config file
            while process.poll() is None:
                time.sleep(1)
                
                # Check for config change
                if Config.SETTINGS_FILE.exists():
                    current_mtime = Config.SETTINGS_FILE.stat().st_mtime
                    if current_mtime > last_config_mtime:
                        print("Config changed. Restarting recorder...")
                        last_config_mtime = current_mtime
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        break
            
            print("FFmpeg exited. Restarting in 2 seconds...")
            time.sleep(2)
        except Exception as e:
            print(f"Error running FFmpeg: {e}")
            time.sleep(10)

if __name__ == "__main__":
    # Flush stdout for Docker logs
    sys.stdout.reconfigure(line_buffering=True)
    record_stream()