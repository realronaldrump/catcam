import os
import re
import shutil
import time
import subprocess
import threading
import logging
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Support both package and direct execution
try:
    from .config import Config
    from .timelapse import generate_timelapse
except ImportError:
    from config import Config
    from timelapse import generate_timelapse

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

def get_version():
    """Gets version string from git commit count and short hash."""
    try:
        # Get commit count
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent
        )
        # Get short hash
        hash_result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent.parent
        )
        
        if count.returncode == 0 and hash_result.returncode == 0:
            return f"v{count.stdout.strip()}.{hash_result.stdout.strip()}"
    except Exception:
        pass
    return "dev"

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

def get_cpu_usage():
    """Reads CPU usage from /proc/stat (Linux only)."""
    try:
        stat_path = Path("/proc/stat")
        if not stat_path.exists():
            return None
        
        with open(stat_path) as f:
            line = f.readline()
        
        parts = line.split()
        if parts[0] != "cpu":
            return None
        
        # user, nice, system, idle, iowait, irq, softirq, steal
        user, nice, system, idle, iowait = map(int, parts[1:6])
        total = user + nice + system + idle + iowait
        idle_pct = (idle + iowait) / total * 100
        return round(100 - idle_pct, 1)
    except:
        return None

def get_memory_usage():
    """Reads memory stats from /proc/meminfo (Linux only)."""
    try:
        meminfo_path = Path("/proc/meminfo")
        if not meminfo_path.exists():
            return None
        
        mem = {}
        with open(meminfo_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(':')
                    val = int(parts[1])  # in kB
                    mem[key] = val
        
        total = mem.get("MemTotal", 0)
        available = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = total - available
        
        return {
            "total_gb": round(total / (1024 * 1024), 1),
            "used_gb": round(used / (1024 * 1024), 1),
            "percent": round(used / total * 100, 1) if total else 0
        }
    except:
        return None

def get_system_uptime():
    """Reads system uptime from /proc/uptime (Linux only)."""
    try:
        uptime_path = Path("/proc/uptime")
        if not uptime_path.exists():
            return None
        
        with open(uptime_path) as f:
            uptime_seconds = float(f.read().split()[0])
        
        days = int(uptime_seconds // 86400)
        hours = int((uptime_seconds % 86400) // 3600)
        minutes = int((uptime_seconds % 3600) // 60)
        
        if days > 0:
            return f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except:
        return None

def get_network_io():
    """Reads network I/O stats from /proc/net/dev (Linux only)."""
    try:
        net_path = Path("/proc/net/dev")
        if not net_path.exists():
            return None
        
        with open(net_path) as f:
            lines = f.readlines()[2:]  # Skip headers
        
        total_rx = 0
        total_tx = 0
        for line in lines:
            parts = line.split()
            iface = parts[0].rstrip(':')
            if iface == "lo":  # Skip loopback
                continue
            total_rx += int(parts[1])
            total_tx += int(parts[9])
        
        return {
            "rx_gb": round(total_rx / (1024**3), 2),
            "tx_gb": round(total_tx / (1024**3), 2)
        }
    except:
        return None

def get_recording_stats(today_path, segment_time):
    """Calculate comprehensive recording statistics for today."""
    stats = {
        "files_today": 0,
        "total_hours": 0,
        "total_size_mb": 0,
        "avg_size_mb": 0,
        "est_bitrate_mbps": None,
        "gaps": [],
        "recent_files": []
    }
    
    try:
        if not today_path.exists():
            return stats
        
        files = sorted(today_path.glob("*.mp4"), key=lambda x: x.stat().st_mtime)
        if not files:
            return stats
        
        stats["files_today"] = len(files)
        
        # Calculate totals
        total_size = sum(f.stat().st_size for f in files)
        stats["total_size_mb"] = round(total_size / (1024 * 1024), 1)
        stats["avg_size_mb"] = round(stats["total_size_mb"] / len(files), 1) if files else 0
        
        # Estimate hours (files * segment time)
        segment_seconds = int(segment_time) if segment_time else 900
        stats["total_hours"] = round(len(files) * segment_seconds / 3600, 1)
        
        # Estimate bitrate from average file size and segment duration
        if stats["avg_size_mb"] > 0 and segment_seconds > 0:
            # bitrate = size_bytes * 8 / duration_seconds / 1_000_000
            avg_bytes = stats["avg_size_mb"] * 1024 * 1024
            stats["est_bitrate_mbps"] = round(avg_bytes * 8 / segment_seconds / 1_000_000, 1)
        
        # Recent files (last 8)
        recent = files[-8:] if len(files) >= 8 else files
        recent.reverse()  # Most recent first
        for f in recent:
            try:
                stats["recent_files"].append({
                    "name": f.name,
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                    "time": datetime.fromtimestamp(f.stat().st_mtime).strftime("%I:%M %p")
                })
            except:
                continue
        
        # Detect gaps (>2x segment time between files)
        threshold = segment_seconds * 2
        for i in range(1, len(files)):
            try:
                prev_mtime = files[i-1].stat().st_mtime
                curr_mtime = files[i].stat().st_mtime
                gap = curr_mtime - prev_mtime - segment_seconds
                
                if gap > threshold:
                    gap_start = datetime.fromtimestamp(prev_mtime + segment_seconds)
                    gap_end = datetime.fromtimestamp(curr_mtime)
                    stats["gaps"].append({
                        "start": gap_start.strftime("%I:%M %p"),
                        "end": gap_end.strftime("%I:%M %p"),
                        "duration_min": round(gap / 60)
                    })
            except:
                continue
        
    except Exception as e:
        logger.error(f"Error calculating recording stats: {e}")
    
    return stats


def get_storage_trend():
    """Get storage usage for the last 7 days."""
    trend = []
    try:
        conf = Config.load()
        base_path = Config.BOX_ROOT / conf["SUBFOLDER"]
        
        for i in range(7):
            day = datetime.now() - timedelta(days=i)
            day_path = base_path / day.strftime("%Y/%m/%d")
            
            if day_path.exists():
                files = list(day_path.glob("*.mp4"))
                total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
                hours = len(files) * int(conf.get("SEGMENT_TIME", 900)) / 3600
            else:
                total_mb = 0
                hours = 0
            
            trend.append({
                "date": day.strftime("%m/%d"),
                "day": day.strftime("%a"),
                "size_gb": round(total_mb / 1024, 2),
                "hours": round(hours, 1),
                "files": len(files) if day_path.exists() else 0
            })
        
    except Exception as e:
        logger.error(f"Error calculating storage trend: {e}")
    
    return trend

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
    
    # New system metrics
    cpu_usage = get_cpu_usage()
    memory = get_memory_usage()
    uptime = get_system_uptime()
    network = get_network_io()
    
    # 2. File & Timeline Logic
    today_path = Config.BOX_ROOT / conf["SUBFOLDER"] / datetime.now().strftime("%Y/%m/%d")
    current_file = "Waiting..."
    current_size = "0.00 MB"
    status_msg = "Idle"
    files_today = 0
    timeline_segments = []
    elapsed_seconds = 0
    
    # Parse segment time with error handling
    try:
        segment_limit_seconds = int(conf.get("SEGMENT_TIME", 900))
    except (ValueError, TypeError) as e:
        logger.warning(f"Failed to parse SEGMENT_TIME '{conf.get('SEGMENT_TIME')}': {e}, using default 900")
        segment_limit_seconds = 900
    
    logger.info(f"Segment limit seconds: {segment_limit_seconds}")
    
    # Get comprehensive recording stats
    recording_stats = get_recording_stats(today_path, segment_limit_seconds)
    
    # Get 7-day storage trend
    storage_trend = get_storage_trend()
    
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
                try:
                    # Parse start time from filename to get actual duration
                    # Format: PM-01-18-00
                    file_time = datetime.strptime(f.stem, "%p-%I-%M-%S").time()
                    start_dt = datetime.combine(datetime.now().date(), file_time)
                    start_ts = start_dt.timestamp()
                    
                    end_ts = f.stat().st_mtime
                    duration = end_ts - start_ts
                    
                    # Sanity check for negative or zero duration
                    if duration <= 0:
                        duration = segment_limit_seconds
                        start_ts = end_ts - duration
                        
                except ValueError:
                    # Fallback if filename parsing fails
                    end_ts = f.stat().st_mtime
                    duration = segment_limit_seconds
                    if f == latest and age < 20:
                        # Use creation time for active file if parsing fails
                        # Note: st_ctime is change time on Linux, but best backward-compatible guess
                        duration = end_ts - f.stat().st_ctime
                    start_ts = end_ts - duration

                left_pct = max(0, ((start_ts - start_of_day) / 86400) * 100)
                width_pct = (duration / 86400) * 100
                timeline_segments.append({"left": f"{left_pct:.2f}%", "width": f"{width_pct:.2f}%"})

    # Build alerts list
    alerts = []
    if disk and disk.get("percent", 0) > 85:
        alerts.append({"type": "warning", "msg": f"Disk usage high: {disk['percent']}%"})
    if not cam_active:
        alerts.append({"type": "error", "msg": "Recorder offline"})
    if not box_active:
        alerts.append({"type": "error", "msg": "Storage mount unavailable"})
    if ping_ms and ping_ms > 100:
        alerts.append({"type": "warning", "msg": f"High camera latency: {ping_ms}ms"})
    for gap in recording_stats.get("gaps", [])[-3:]:  # Last 3 gaps only
        alerts.append({"type": "info", "msg": f"Gap: {gap['start']} - {gap['end']} ({gap['duration_min']}min)"})

    return JSONResponse({
        # Original fields
        "cam_active": cam_active, 
        "box_active": box_active,
        "current_file": current_file, 
        "current_size": current_size, 
        "status_msg": status_msg,
        "disk": disk, 
        "files_today": files_today,
        "cpu_temp": cpu_temp, 
        "ping_ms": ping_ms, 
        "timeline": timeline_segments,
        "elapsed_seconds": elapsed_seconds, 
        "segment_limit_seconds": segment_limit_seconds,
        
        # New system metrics
        "cpu_usage": cpu_usage,
        "memory": memory,
        "uptime": uptime,
        "network": network,
        
        # Enhanced recording stats
        "recording": recording_stats,
        
        # 7-day trend
        "storage_trend": storage_trend,
        
        # Alerts
        "alerts": alerts
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
        "segment_seconds": seconds,
        "version": get_version()
    })

@app.post("/settings")
async def save_settings(request: Request, 
                      subfolder: str = Form(...), 
                      segment_minutes: int = Form(...),
                      segment_seconds: int = Form(...),
                      camera_ip: str = Form(...), 
                      camera_user: str = Form(...), 
                      camera_pass: str = Form(...),
                      enable_audio: bool = Form(False)):
    
    # Convert minutes/seconds back to total seconds
    total_seconds = (segment_minutes * 60) + segment_seconds
    
    data = {
        "SUBFOLDER": subfolder, 
        "SEGMENT_TIME": str(total_seconds), 
        "CAMERA_IP": camera_ip, 
        "CAMERA_USER": camera_user, 
        "CAMERA_PASS": camera_pass,
        "ENABLE_AUDIO": str(enable_audio)
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
        "segment_seconds": segment_seconds,
        "version": get_version()
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

@app.get("/audio_feed")
async def audio_feed():
    """Streams audio from the camera as MP3."""
    url = Config.get_rtsp_url()
    
    # Use ffmpeg to extract audio and encode to MP3 standard output
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-rtsp_transport", "tcp",
        "-timeout", "5000000",
        "-i", url,
        "-vn",              # weirdly enough, we want no video
        "-f", "mp3",        # format mp3
        "-c:a", "libmp3lame", # encoder
        "-ab", "128k",      # bitrate
        "-ac", "2",         # channels
        "-ar", "44100",     # sample rate
        "-"                 # output to pipe
    ]
    
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate_audio():
        try:
            while True:
                data = process.stdout.read(4096)
                if not data:
                    break
                yield data
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except:
                process.kill()

    return StreamingResponse(generate_audio(), media_type="audio/mpeg")

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