# CatCam System Documentation
**Last Updated:** December 2025
**Repository:** `https://github.com/realronaldrump/catcam`

## 1. System Overview
CatCam is a custom Network Video Recorder (NVR) application designed to record a 24/7 RTSP stream from a Reolink camera, segment the video into 15-minute chunks, and upload them immediately to Box.com cloud storage. It includes a web dashboard for live viewing, playback, and system monitoring.

## 2. Infrastructure & Connectivity

### Hardware
*   **Host Machine:** Mini PC running Ubuntu 24.04 LTS.
*   **Camera:** Reolink IP Camera.
    *   **Local IP:** `192.168.1.58` (Static/DHCP reservation recommended).
    *   **Protocol:** RTSP (TCP transport).

### Network Access
*   **Remote Access:** The Mini PC is connected via **Tailscale**.
*   **Tailscale IP:** `100.108.79.105`
*   **SSH User:** `davis`
*   **SSH Command:** `ssh davis@100.108.79.105`

### Storage Architecture (Critical)
The system does **not** store video on the local disk permanently. It mounts Box.com as a local filesystem.
*   **Tool:** `rclone`
*   **Mount Point (Host):** `/home/davis/Box`
*   **Systemd Service:** `rclone-box.service`
*   **Docker Mapping:** Host `/home/davis/Box` is mounted to `/data/box` inside containers.

## 3. Docker Architecture
The application runs via Docker Compose with four distinct services:

| Service | Container Name | Function |
| :--- | :--- | :--- |
| **web** | `catcam_web` | FastAPI web server (Port 2121). Handles the dashboard, live feed proxy, and settings. |
| **recorder** | `catcam_recorder` | Runs an infinite Python/FFmpeg loop. Captures RTSP stream and saves `.mp4` files directly to `/data/box`. |
| **timelapser** | `catcam_timelapser` | Runs once daily (or on demand) to generate a timelapse of the previous day. |
| **webhook** | `catcam_webhook` | Listens on Port 9000 for GitHub push events. Triggers `scripts/deploy.sh` to auto-update the app. |

## 4. Configuration

### Environment Variables (`.env`)
Located at `~/catcam/.env`. Contains secrets not committed to GitHub.
```bash
WEBHOOK_SECRET=your_github_secret_here
```

### App Settings (`config/settings.env`)
Located in the Docker volume `catcam_config`. Can be edited via the Web Dashboard (**Settings** tab).
*   `CAMERA_IP`: 192.168.1.58
*   `CAMERA_USER`: admin
*   `CAMERA_PASS`: [Hidden]
*   `SEGMENT_TIME`: 900 (15 minutes)
*   `SUBFOLDER`: Other/CatCam

## 5. Deployment Workflow (CI/CD)
The system is set up for **Continuous Deployment**:
1.  User pushes code to `main` branch on GitHub.
2.  GitHub sends a payload to `http://100.108.79.105:9000/hooks/deploy`.
3.  The `catcam_webhook` container verifies the signature using `WEBHOOK_SECRET`.
4.  It executes `scripts/deploy.sh` inside the container (which mounts the host Docker socket).
5.  The script pulls the latest git changes and runs `docker compose up -d --build`.

## 6. Operational Commands (Cheat Sheet)

### Accessing Logs
```bash
cd ~/catcam
# View all logs
docker compose logs -f
# View just the recorder (ffmpeg)
docker compose logs -f recorder
```

### Restarting the App
```bash
cd ~/catcam
docker compose restart
```

### Checking Storage Mount
If the app says "Storage Error" or disk usage is high, check the mount:
```bash
df -h /home/davis/Box
# Should show "Box:" or a size like 1.0P. 
# If it shows /dev/sda, the mount is broken.
```

### Fixing the Mount (If broken)
```bash
sudo systemctl restart rclone-box.service
```

### Checking Disk Usage
If the local disk fills up (usually due to upload caching issues):
```bash
# Check rclone cache size
du -sh /home/davis/.cache/rclone

# Clear rclone cache
rm -rf /home/davis/.cache/rclone/*
```

## 7. Rclone Service Configuration
The system relies on a custom Systemd service to keep Box mounted.
**File:** `/etc/systemd/system/rclone-box.service`

**Key Configuration Flags:**
*   `--allow-other`: **Crucial.** Allows Docker containers to see the mount.
*   `--vfs-cache-mode full`: **Crucial.** Prevents FFmpeg crashes by caching writes locally before uploading.

## 8. Troubleshooting History (Known Issues)

### "Input/output error" or "Permission Denied"
*   **Cause:** The Box mount has disconnected, or Docker started before the mount was ready.
*   **Fix:**
    1. `sudo systemctl restart rclone-box.service`
    2. `cd ~/catcam && docker compose restart`

### "No route to host" (FFmpeg logs)
*   **Cause:** The camera IP address changed or the camera is offline.
*   **Fix:** Check camera IP in router/app. Update IP in the CatCam Web Dashboard > Settings.

### Disk Space Filling Up
*   **Cause:** Rclone cache is filling up because uploads are slower than recordings, OR the mount failed and the app is writing to the local `/home/davis/Box` folder.
*   **Fix:**
    1. Check mount (`df -h`).
    2. If mounted, clear cache (`rm -rf ~/.cache/rclone/*`).
    3. If NOT mounted, stop docker, clear `/home/davis/Box`, remount, start docker.