import os
from pathlib import Path

class Config:
    # Storage
    # Storage
    BOX_ROOT = Path("/data/box")
    
    # Use /config if it exists (Docker), otherwise use local ./config
    if Path("/config").exists():
        CONFIG_DIR = Path("/config")
    else:
        CONFIG_DIR = Path("config")
        
    SETTINGS_FILE = CONFIG_DIR / "settings.env"

    @classmethod
    def load(cls):
        """Loads settings from file, overriding defaults."""
        defaults = {
            "SUBFOLDER": os.getenv("SUBFOLDER", "Other/CatCam"),
            "SEGMENT_TIME": os.getenv("SEGMENT_TIME", "900"),
            "CAMERA_IP": os.getenv("CAMERA_IP", "192.168.1.163"),
            "CAMERA_USER": os.getenv("CAMERA_USER", "admin"),
            "CAMERA_PASS": os.getenv("CAMERA_PASS", "CoonCam19"),
            "TIMELAPSE_OUTPUT_DIR": os.getenv("TIMELAPSE_OUTPUT_DIR", "Timelapses"),
        }
        
        if cls.SETTINGS_FILE.exists():
            with open(cls.SETTINGS_FILE, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        defaults[k] = v.strip('"')
        
        return defaults

    @classmethod
    def get_rtsp_url(cls):
        conf = cls.load()
        return f"rtsp://{conf['CAMERA_USER']}:{conf['CAMERA_PASS']}@{conf['CAMERA_IP']}:554/h264Preview_01_main"
