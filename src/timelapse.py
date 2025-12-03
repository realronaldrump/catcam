import time
import subprocess
import sys
import os
from datetime import datetime, timedelta
from pathlib import Path
from .config import Config

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

def get_yesterday_date():
    """Returns yesterday's date object."""
    return datetime.now() - timedelta(days=1)

def get_seconds_until_next_run():
    """Calculates seconds remaining until 12:05 AM tomorrow (or today if currently before 12:05 AM)."""
    now = datetime.now()
    target = now.replace(hour=0, minute=5, second=0, microsecond=0)
    
    if now >= target:
        # If it's already past 12:05 AM, schedule for tomorrow
        target += timedelta(days=1)
        
    return int((target - now).total_seconds())

def generate_timelapse(target_date=None, force=False):
    """
    Main logic to generate the timelapse.
    Args:
        target_date (datetime.date, optional): The date to generate for. Defaults to yesterday.
        force (bool): If True, overwrites existing timelapse.
    Returns:
        dict: {"success": bool, "message": str}
    """
    if not wait_for_box():
        return {"success": False, "message": "Box mount failed or timed out."}

    conf = Config.load()
    
    if target_date is None:
        # Default to yesterday if running automatically
        target_date = get_yesterday_date().date()
    
    # Ensure target_date is a date object (if passed as datetime)
    if isinstance(target_date, datetime):
        target_date = target_date.date()

    # Source directory: /data/box/Other/CatCam/YYYY/MM/DD
    source_dir = Config.BOX_ROOT / conf["SUBFOLDER"] / target_date.strftime("%Y/%m/%d")
    
    # Output directory: /data/box/Other/CatCam/Timelapses
    output_dir = Config.BOX_ROOT / conf["SUBFOLDER"] / conf["TIMELAPSE_OUTPUT_DIR"]
    output_dir.mkdir(parents=True, exist_ok=True)
    
    output_file = output_dir / f"{target_date.strftime('%Y-%m-%d')}.mp4"

    # Check if exists
    if output_file.exists() and not force:
        msg = f"Timelapse already exists for {target_date}. Skipping."
        print(msg)
        return {"success": True, "message": msg}

    print(f"Checking for recordings in {source_dir}...")
    
    if not source_dir.exists():
        msg = f"No folder found for date: {target_date}"
        print(msg)
        return {"success": False, "message": msg}

    # 1. Scan and Sort
    files = sorted(list(source_dir.glob("*.mp4")))
    if not files:
        msg = f"No .mp4 files found for {target_date}."
        print(msg)
        return {"success": False, "message": msg}
    
    print(f"Found {len(files)} videos.")

    # 2. Create Playlist
    playlist_path = source_dir / "playlist.txt"
    try:
        with open(playlist_path, "w") as f:
            for video in files:
                # FFmpeg concat requires 'file ' prefix and safe paths
                # Since we are running locally, absolute paths are fine.
                f.write(f"file '{video.absolute()}'\n")
        
        print(f"Created playlist at {playlist_path}")

        # 3. Run FFmpeg
        print(f"Starting timelapse generation -> {output_file}")
        
        cmd = [
            "ffmpeg",
            "-f", "concat",
            "-safe", "0",
            "-i", str(playlist_path),
            "-filter:v", "setpts=0.01*PTS",
            "-an",
            "-c:v", "libx264",       # FORCE standardized H.264 encoding
            "-pix_fmt", "yuv420p",   # FORCE compatibility with all players (QuickTime/iOS)
            "-preset", "fast",       # Balance speed/compression
            "-y", # Overwrite output if exists
            str(output_file)
        ]
        
        start_time = time.time()
        subprocess.run(cmd, check=True)
        duration = time.time() - start_time
        
        msg = f"Timelapse created successfully in {duration:.2f} seconds!"
        print(msg)
        return {"success": True, "message": msg}

    except subprocess.CalledProcessError as e:
        msg = f"FFmpeg failed: {e}"
        print(msg)
        return {"success": False, "message": msg}
    except Exception as e:
        msg = f"Error during timelapse generation: {e}"
        print(msg)
        return {"success": False, "message": msg}
    finally:
        # 4. Cleanup
        if playlist_path.exists():
            playlist_path.unlink()
            print("Cleaned up playlist.txt")

def main():
    # Flush stdout for Docker logs
    sys.stdout.reconfigure(line_buffering=True)
    
    print("Timelapse worker started.")
    
    # Initial run check (optional, or just go to sleep loop)
    # For robust daemon behavior, we enter the loop immediately.
    
    while True:
        seconds_to_sleep = get_seconds_until_next_run()
        next_run_time = datetime.now() + timedelta(seconds=seconds_to_sleep)
        
        print(f"Sleeping for {seconds_to_sleep} seconds. Next run at {next_run_time}")
        time.sleep(seconds_to_sleep)
        
        print("Waking up for daily timelapse job...")
        generate_timelapse()

if __name__ == "__main__":
    main()
