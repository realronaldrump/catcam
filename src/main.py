import os
import re
import shutil
import time
import subprocess
import threading
import logging
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from .config import Config
from .timelapse import generate_timelapse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Security Helpers ---

def validate_date_param(date_str: str) -> bool:
    """Validates date parameter matches YYYY-MM-DD format and is a valid date."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return False
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        return True
    except ValueError:
        return False

app = FastAPI()
templates = Jinja2Templates(directory="src/templates")

# --- Singleton Video Camera ---
class VideoCamera:
    def __init__(self):
        self.frame = None
        self.last_frame_time = 0
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()

    def _capture_loop(self):
        import cv2
        logger.info("Starting video capture loop...")
        while True:
            try:
                url = Config.get_rtsp_url()
                cap = cv2.VideoCapture(url)
                
                if not cap.isOpened():
                    logger.error(f"Failed to open RTSP stream: {url}")
                    time.sleep(5)
                    continue

                while True:
                    success, frame = cap.read()
                    if not success:
                        logger.warning("Failed to read frame from stream. Reconnecting...")
                        break
                    
                    # Resize and encode once
                    frame = cv2.resize(frame, (640, 360))
                    ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
                    
                    if ret:
                        with self.lock:
                            self.frame = buffer.tobytes()
                            self.last_frame_time = time.time()
                    
                    # Limit capture rate to ~15fps to save CPU if needed, 
                    # but blocking read() usually handles timing.
                    # time.sleep(0.05) 

                cap.release()
            except Exception as e:
                logger.error(f"Error in video capture loop: {e}")
            
            time.sleep(2) # Wait before reconnecting

    def get_frame(self):
        # Return the last known frame
        with self.lock:
            if self.frame and (time.time() - self.last_frame_time) < 5:
                return self.frame
        return None

# Global camera instance
camera = VideoCamera()

# --- Helpers ---

def get_disk_usage():
    """Checks disk usage of the mini PC's root filesystem."""
    try:
        # Measure the mini PC's root filesystem to ensure videos aren't filling up local disk
        total, used, free = shutil.disk_usage("/")
        percent_used = (used / total) * 100
        return {"percent": round(percent_used, 1), "free_gb": round(free / (1024**3), 1)}
    except Exception:
        return {"percent": 0, "free_gb": 0}

def get_cpu_temp():
    """Reads Linux thermal zone (mounted read-only)."""
    try:
        base = Path("/sys/class/thermal")
        if not base.exists():
            return "--"

        # Preferred zones that usually represent the CPU core/package
        preferred_types = ["x86_pkg_temp", "coretemp", "k10temp", "cpu-thermal"]
        
        # 1. Scan for a preferred zone
        for z in base.glob("thermal_zone*"):
            try:
                type_path = z / "type"
                if type_path.exists():
                    z_type = type_path.read_text().strip()
                    if z_type in preferred_types:
                        # Found a good one!
                        temp_c = int((z / "temp").read_text().strip()) / 1000
                        return f"{temp_c * 9/5 + 32:.1f}°F"
            except: continue

        # 2. Fallback to thermal_zone0 (original behavior)
        tz0 = base / "thermal_zone0" / "temp"
        if tz0.exists():
            temp_c = int(tz0.read_text().strip()) / 1000
            return f"{temp_c * 9/5 + 32:.1f}°F"
            
    except Exception:
        pass
    return "--"

def get_camera_ping(ip):
    """Simple ping to check connection quality."""
    try:
        # Docker container needs 'iputils-ping' installed
        res = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True)
        if "time=" in res.stdout:
            ms = res.stdout.split("time=")[1].split(" ")[0]
            return float(ms)
    except: pass
    return None

def get_recorder_status():
    """Checks if new files are being written to verify recorder health."""
    try:
        conf = Config.load()
        today_path = Config.BOX_ROOT / conf["SUBFOLDER"] / datetime.now().strftime("%Y/%m/%d")
        if not today_path.exists():
            return False
        
        files = list(today_path.glob("*.mp4"))
        if not files:
            return False
            
        latest = max(files, key=lambda x: x.stat().st_mtime)
        age = time.time() - latest.stat().st_mtime
        return age < 60 # Considered active if wrote in last minute
    except:
        return False

# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "page": "dashboard"})

@app.get("/api/stats")
async def api_stats():
    # 1. System Vitals
    conf = Config.load()
    cam_active = get_recorder_status()
    box_active = Config.BOX_ROOT.exists() and os.access(Config.BOX_ROOT, os.R_OK)
    disk = get_disk_usage()
    cpu_temp = get_cpu_temp()
    ping_ms = get_camera_ping(conf["CAMERA_IP"])
    
    # 2. File & Timeline Logic
    today_path = Config.BOX_ROOT / conf["SUBFOLDER"] / datetime.now().strftime("%Y/%m/%d")
    current_file = "Waiting..."
    current_size = "0.00 MB"
    status_msg = "Idle"
    files_today = 0
    timeline_segments = []
    elapsed_seconds = 0
    segment_limit_seconds = int(conf.get("SEGMENT_TIME", 900))
    
    if today_path.exists():
        files = list(today_path.glob("*.mp4"))
        files_today = len(files)
        if files:
            files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            latest = files[0]
            age = time.time() - latest.stat().st_mtime
            current_file = latest.name
            current_size = f"{(latest.stat().st_size / (1024*1024)):.2f} MB"
            status_msg = "Recording (Active)" if age < 20 else f"Last write: {int(age)}s ago"
            
            # Calculate elapsed time from filename if active
            if age < 20:
                try:
                    # Filename format: PM-01-18-00.mp4
                    # We need to combine with today's date to get full timestamp
                    time_str = latest.stem # PM-01-18-00
                    # Parse: %p-%I-%M-%S
                    file_time = datetime.strptime(time_str, "%p-%I-%M-%S").time()
                    file_dt = datetime.combine(datetime.now().date(), file_time)
                    elapsed_seconds = int((datetime.now() - file_dt).total_seconds())
                except ValueError:
                    pass # Fallback to 0 if format doesn't match

            # Build Timeline Data
            start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            
            for f in files:
                end_ts = f.stat().st_mtime
                duration = segment_limit_seconds # Default assumption
                if f == latest and age < 20:
                    duration = end_ts - f.stat().st_ctime
                    if duration < 0: duration = 60
                
                start_ts = end_ts - duration
                left_pct = max(0, ((start_ts - start_of_day) / 86400) * 100)
                width_pct = (duration / 86400) * 100
                timeline_segments.append({"left": f"{left_pct:.2f}%", "width": f"{width_pct:.2f}%"})

    # Logs (Not easily accessible from container to host journal, skipping for now or reading local log file if we add one)
    logs = "Logs unavailable in container mode (check 'docker logs')"

    return JSONResponse({
        "cam_active": cam_active, "box_active": box_active,
        "current_file": current_file, "current_size": current_size, "status_msg": status_msg,
        "disk": disk, "logs": logs, "files_today": files_today,
        "cpu_temp": cpu_temp, "ping_ms": ping_ms, "timeline": timeline_segments,
        "elapsed_seconds": elapsed_seconds, "segment_limit_seconds": segment_limit_seconds
    })

@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, date: str = None):
    conf = Config.load()
    if not date: 
        date = datetime.now().strftime("%Y-%m-%d")
    
    # SECURITY FIX: Validate date format to prevent directory traversal
    if not validate_date_param(date):
        return HTMLResponse("Invalid date parameter", 400)
    
    path_date = date.replace("-", "/")
    target_dir = Config.BOX_ROOT / conf["SUBFOLDER"] / path_date
    
    # Additional safety check: ensure resolved path is within expected base
    try:
        resolved = target_dir.resolve()
        expected_base = (Config.BOX_ROOT / conf["SUBFOLDER"]).resolve()
        if not str(resolved).startswith(str(expected_base)):
            return HTMLResponse("Access Denied", 403)
    except Exception:
        return HTMLResponse("Invalid path", 400)
    
    videos = []
    if target_dir.exists():
        for f in sorted(target_dir.glob("*.mp4")):
            # Create a relative path for the player
            try:
                rel_path = f.relative_to(Config.BOX_ROOT)
                thumb_path = f.with_suffix('.thumb.jpg')
                thumb_rel = thumb_path.relative_to(Config.BOX_ROOT) if thumb_path.exists() else None
                
                videos.append({
                    "name": f.name, 
                    "size": f"{round(f.stat().st_size/(1024*1024),1)} MB", 
                    "path": str(rel_path),
                    "thumb": str(thumb_rel) if thumb_rel else None
                })
            except ValueError:
                pass # Should not happen if BOX_ROOT is correct
    return templates.TemplateResponse("index.html", {"request": request, "page": "library", "current_date": date, "videos": videos})

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    conf = Config.load()
    # Convert total seconds to minutes and seconds for display
    total_seconds = int(conf.get("SEGMENT_TIME", 900))
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "page": "settings", 
        "config": conf,
        "segment_minutes": minutes,
        "segment_seconds": seconds
    })

@app.post("/settings")
async def save_settings(request: Request, 
                      subfolder: str = Form(...), 
                      segment_minutes: int = Form(...),
                      segment_seconds: int = Form(...),
                      camera_ip: str = Form(...), 
                      camera_user: str = Form(...), 
                      camera_pass: str = Form(...)):
    
    # Convert minutes/seconds back to total seconds
    total_seconds = (segment_minutes * 60) + segment_seconds
    
    data = {
        "SUBFOLDER": subfolder, 
        "SEGMENT_TIME": str(total_seconds), 
        "CAMERA_IP": camera_ip, 
        "CAMERA_USER": camera_user, 
        "CAMERA_PASS": camera_pass
    }
    
    # Save to persistent volume
    Config.CONFIG_DIR.mkdir(exist_ok=True)
    with open(Config.SETTINGS_FILE, "w") as f:
        for key, val in data.items():
            f.write(f'{key}="{val}"\n')
            
    msg = "Settings saved! Services will restart shortly."
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "page": "settings", 
        "config": data, 
        "message": msg,
        "segment_minutes": segment_minutes,
        "segment_seconds": segment_seconds
    })

@app.get("/video_feed")
async def video_feed():
    def gen():
        while True:
            frame = camera.get_frame()
            if frame:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
                time.sleep(0.06) # ~15 FPS
            else:
                time.sleep(0.1) # Wait for frame
                
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/play_file/{file_path:path}")
async def play_file(file_path: str):
    # Securely serve files from BOX_ROOT
    safe_path = (Config.BOX_ROOT / file_path).resolve()
    if not str(safe_path).startswith(str(Config.BOX_ROOT.resolve())):
        return HTMLResponse("Access Denied", 403)
    if not safe_path.exists():
        return HTMLResponse("File not found", 404)
    return FileResponse(safe_path, media_type="video/mp4")

@app.get("/thumb/{file_path:path}")
async def serve_thumbnail(file_path: str):
    """Securely serve thumbnail images from BOX_ROOT."""
    safe_path = (Config.BOX_ROOT / file_path).resolve()
    if not str(safe_path).startswith(str(Config.BOX_ROOT.resolve())):
        return HTMLResponse("Access Denied", 403)
    if not safe_path.exists():
        return HTMLResponse("Thumbnail not found", 404)
    return FileResponse(safe_path, media_type="image/jpeg")

@app.get("/timelapses", response_class=HTMLResponse)
async def timelapses(request: Request):
    conf = Config.load()
    output_dir = Config.BOX_ROOT / conf["SUBFOLDER"] / conf["TIMELAPSE_OUTPUT_DIR"]
    
    videos = []
    if output_dir.exists():
        for f in sorted(output_dir.glob("*.mp4"), reverse=True): # Newest first
            try:
                rel_path = f.relative_to(Config.BOX_ROOT)
                # Timelapses don't have thumbnails generated yet, but we could add that later.
                # For now, we'll just show the video link.
                
                videos.append({
                    "name": f.name, 
                    "size": f"{round(f.stat().st_size/(1024*1024),1)} MB", 
                    "path": str(rel_path),
                    "thumb": None # Placeholder
                })
            except ValueError:
                pass

    return templates.TemplateResponse("index.html", {
        "request": request, 
        "page": "timelapses", 
        "videos": videos,
        "today": datetime.now().strftime("%Y-%m-%d")
    })

@app.post("/api/generate_timelapse")
async def api_generate_timelapse(background_tasks: BackgroundTasks, date: str = Form(...), force: bool = Form(False)):
    # Validate date
    if not validate_date_param(date):
        return JSONResponse({"success": False, "message": "Invalid date format"}, status_code=400)
    
    target_date = datetime.strptime(date, "%Y-%m-%d").date()
    
    # Prevent future dates
    if target_date >= datetime.now().date():
        return JSONResponse({"success": False, "message": "Cannot generate timelapse for today or future dates."}, status_code=400)

    # Add to background tasks
    background_tasks.add_task(generate_timelapse, target_date=target_date, force=force)
    
    return JSONResponse({"success": True, "message": f"Timelapse generation started for {date}. Check back in a few minutes."})