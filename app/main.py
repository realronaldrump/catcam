import os
import shutil
import time
import subprocess
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

# Configuration from Environment
BOX_ROOT = Path("/data/Box")
SUBFOLDER = os.getenv("SUBFOLDER", "Other/CatCam")
CAMERA_IP = os.getenv("CAMERA_IP", "192.168.1.163")
CAMERA_USER = os.getenv("CAMERA_USER", "admin")
CAMERA_PASS = os.getenv("CAMERA_PASS", "CoonCam19")
SEGMENT_TIME = os.getenv("SEGMENT_TIME", "900")

def get_config():
    return {
        "SUBFOLDER": SUBFOLDER,
        "SEGMENT_TIME": SEGMENT_TIME,
        "CAMERA_IP": CAMERA_IP,
        "CAMERA_USER": CAMERA_USER,
        "CAMERA_PASS": CAMERA_PASS
    }

def get_service_status(service_name):
    # In Docker/Supervisord, we can assume if this app is running, the "web" part is active.
    # For the recorder, we could check if the python process is running.
    if service_name == "catcam.service":
        # Check if recorder.py is running
        try:
            res = subprocess.run(["pgrep", "-f", "app.recorder"], capture_output=True)
            return res.returncode == 0
        except: return False
    return True # Box mount is handled by volume

def get_cpu_temp():
    # Might not work in container without --privileged or volume mount of /sys
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp_c = int(f.read()) / 1000
            return f"{temp_c * 9/5 + 32:.1f}°F"
    except: return "--"

def get_camera_ping(ip):
    try:
        # Ping might need to be installed in the container
        res = subprocess.run(["ping", "-c", "1", "-W", "1", ip], capture_output=True, text=True)
        if "time=" in res.stdout:
            ms = res.stdout.split("time=")[1].split(" ")[0]
            return float(ms)
    except: pass
    return None

def get_disk_usage():
    try:
        total, used, free = shutil.disk_usage(BOX_ROOT)
        percent_used = (used / total) * 100
        return {"percent": round(percent_used, 1), "free_gb": round(free / (1024**3), 1)}
    except:
        return {"percent": 0, "free_gb": 0}

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "page": "dashboard"})

@app.get("/api/stats")
async def api_stats():
    # 1. System Vitals
    cam_active = get_service_status("catcam.service")
    box_active = BOX_ROOT.exists() and os.access(BOX_ROOT, os.W_OK)
    
    # Disk: Handle "unlimited" Box storage gracefully
    disk = get_disk_usage()
    if disk["free_gb"] > 10000: # If > 10TB, it's likely cloud/unlimited
        disk_text = f"{disk['percent']}% (Cloud)"
    else:
        disk_text = f"{disk['percent']}% ({disk['free_gb']} GB free)"

    cpu_temp = get_cpu_temp()
    ping_ms = get_camera_ping(CAMERA_IP)
    
    # 2. File & Timeline Logic
    today_path = BOX_ROOT / SUBFOLDER / datetime.now().strftime("%Y/%m/%d")
    current_file = "Waiting..."
    current_size = "0.00 MB"
    status_msg = "Idle"
    files_today = 0
    timeline_segments = []
    
    if today_path.exists():
        files = list(today_path.glob("*.mp4"))
        files_today = len(files)
        if files:
            files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            latest = files[0]
            age = time.time() - latest.stat().st_mtime
            current_file = latest.name
            current_size = f"{(latest.stat().st_size / (1024*1024)):.2f} MB"
            
            # If age is high, maybe we are just not writing?
            if age < 30:
                status_msg = "Recording (Active)"
            else:
                status_msg = f"Last write: {int(age)}s ago"
            
            start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            
            for f in files:
                end_ts = f.stat().st_mtime
                duration = 900 # default
                if f == latest and age < 30:
                    duration = end_ts - f.stat().st_ctime
                    if duration < 0: duration = 60
                
                start_ts = end_ts - duration
                left_pct = max(0, ((start_ts - start_of_day) / 86400) * 100)
                width_pct = (duration / 86400) * 100
                
                timeline_segments.append({"left": f"{left_pct:.2f}%", "width": f"{width_pct:.2f}%"})

    # Logs: Read from the shared log file
    try:
        # Read last 50 lines
        proc = subprocess.run(["tail", "-n", "50", "/app/catcam.log"], capture_output=True, text=True)
        logs = proc.stdout
    except Exception as e:
        logs = f"Error reading logs: {e}"
    
    # Uptime
    try:
        with open('/proc/uptime', 'r') as f:
            uptime = f"{int(float(f.readline().split()[0]) / 3600)}h"
    except: uptime = "?"

    return JSONResponse({
        "cam_active": cam_active, "box_active": box_active,
        "current_file": current_file, "current_size": current_size, "status_msg": status_msg,
        "disk": {"percent": disk["percent"], "text": disk_text}, 
        "logs": logs, "files_today": files_today, "uptime": uptime,
        "cpu_temp": cpu_temp, "ping_ms": ping_ms, "timeline": timeline_segments
    })

@app.get("/library", response_class=HTMLResponse)
async def library(request: Request, date: str = None):
    if not date: date = datetime.now().strftime("%Y-%m-%d")
    path_date = date.replace("-", "/")
    target_dir = BOX_ROOT / SUBFOLDER / path_date
    videos = []
    if target_dir.exists():
        for f in sorted(target_dir.glob("*.mp4")):
            videos.append({"name": f.name, "size": f"{round(f.stat().st_size/(1024*1024),1)} MB", "path": str(f)})
    return templates.TemplateResponse("index.html", {"request": request, "page": "library", "current_date": date, "videos": videos})

@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "page": "settings", "config": get_config()})

@app.get("/video_feed")
async def video_feed():
    import cv2
    url = f"rtsp://{CAMERA_USER}:{CAMERA_PASS}@{CAMERA_IP}:554/h264Preview_01_main"
    def gen():
        cap = cv2.VideoCapture(url)
        while True:
            success, frame = cap.read()
            if not success:
                time.sleep(2)
                cap = cv2.VideoCapture(url)
                continue
            frame = cv2.resize(frame, (640, 360))
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 60])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/play_file")
async def play_file(path: str):
    # Security check: ensure path is within BOX_ROOT
    p = Path(path).resolve()
    if not str(p).startswith(str(BOX_ROOT.resolve())):
        return HTMLResponse("Access Denied", 403)
    return FileResponse(p, media_type="video/mp4")
