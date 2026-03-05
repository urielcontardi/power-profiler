"""
ProfilerManager — manages IoT sensor config sequencing with event CSV logging.

Reuses IoTClient logic from power_profiler/run_tests.py.
"""

import csv
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

EXPECTED_FW_PREFIX = "ST3001"


# ---------------------------------------------------------------------------
# IoT API Client
# ---------------------------------------------------------------------------
class IoTClient:
    def __init__(self, base_url: str, user_id: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "accept": "application/json",
            "x-user-id": user_id,
        })

    def _get(self, path: str) -> Any:
        r = self.session.get(f"{self.base_url}{path}", timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> Any:
        r = self.session.post(f"{self.base_url}{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_last_status(self, sensor_id: str) -> dict | None:
        try:
            data = self._get(f"/v1/smarttrac/{sensor_id}/status/last")
            return data.get("statusV3") or data.get("statusV2") or data.get("statusV1")
        except Exception:
            return None

    def get_config(self, sensor_id: str) -> dict:
        return self._get(f"/v1/smarttrac/{sensor_id}/config/v3/raw")

    def post_config(self, sensor_id: str, body: dict) -> dict:
        return self._post(f"/v1/smarttrac/{sensor_id}/config/v3/raw", body)


# ---------------------------------------------------------------------------
# ProfilerManager
# ---------------------------------------------------------------------------
class ProfilerManager:
    _SETTINGS_FILE = "profiler_settings.json"
    _SEQUENCE_FILE = "profiler_sequence.json"
    _CONFIGS_DIR = "profiler_configs"

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / self._CONFIGS_DIR).mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        # Runtime state (protected by _lock)
        self._running = False
        self._current_step: str = ""
        self._current_config: str = ""
        self._sensor_status: dict[str, str] = {}
        self._events_file: str = ""
        self._log_lines: list[str] = []
        self._start_time: str = ""

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def get_settings(self) -> dict:
        path = self.log_dir / self._SETTINGS_FILE
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "base_url": "https://iot.int.tractian.com",
            "user_id": "",
            "sensor_ids": [],
            "check_firmware": True,
            "retry_interval_minutes": 1,
        }

    def save_settings(self, data: dict) -> dict:
        path = self.log_dir / self._SETTINGS_FILE
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"success": True}

    # ------------------------------------------------------------------
    # Config files
    # ------------------------------------------------------------------
    def _configs_dir(self) -> Path:
        return self.log_dir / self._CONFIGS_DIR

    def list_configs(self) -> list[dict]:
        result = []
        for f in sorted(self._configs_dir().glob("*.json")):
            result.append({"name": f.name, "size": f.stat().st_size})
        return result

    def save_config(self, name: str, content_bytes: bytes) -> dict:
        safe_name = Path(name).name
        if not safe_name.endswith(".json"):
            return {"error": "Only .json files allowed"}
        (self._configs_dir() / safe_name).write_bytes(content_bytes)
        return {"success": True, "name": safe_name}

    def delete_config(self, name: str) -> dict:
        safe_name = Path(name).name
        path = self._configs_dir() / safe_name
        if not path.exists():
            return {"error": "File not found"}
        path.unlink()
        return {"success": True}

    def get_config_content(self, name: str) -> dict | None:
        safe_name = Path(name).name
        path = self._configs_dir() / safe_name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Sequence
    # ------------------------------------------------------------------
    def get_sequence(self) -> dict:
        path = self.log_dir / self._SEQUENCE_FILE
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "name": "Power Profiler Test",
            "check_firmware": True,
            "retry_interval_minutes": 1,
            "steps": [],
        }

    def save_sequence(self, seq: dict) -> dict:
        path = self.log_dir / self._SEQUENCE_FILE
        path.write_text(json.dumps(seq, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"success": True}

    # ------------------------------------------------------------------
    # Run control
    # ------------------------------------------------------------------
    def start_run(self) -> dict:
        with self._lock:
            if self._running:
                return {"error": "Already running"}

        settings = self.get_settings()
        sequence = self.get_sequence()

        if not settings.get("sensor_ids"):
            return {"error": "No sensor IDs configured"}
        if not sequence.get("steps"):
            return {"error": "No steps in sequence"}

        self._stop_event.clear()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        events_file = str(self.log_dir / f"events_{ts}.csv")

        with self._lock:
            self._running = True
            self._start_time = datetime.now().isoformat()
            self._current_step = "Starting..."
            self._current_config = ""
            self._sensor_status = {sid: "Waiting..." for sid in settings["sensor_ids"]}
            self._events_file = events_file
            self._log_lines = []

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(settings, sequence, events_file),
            daemon=True,
        )
        self._thread.start()
        return {"success": True, "events_file": os.path.basename(events_file)}

    def stop_run(self) -> dict:
        self._stop_event.set()
        return {"success": True}

    def get_status(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "start_time": self._start_time,
                "current_step": self._current_step,
                "current_config": self._current_config,
                "sensor_status": dict(self._sensor_status),
                "events_file": os.path.basename(self._events_file) if self._events_file else "",
            }

    def get_log_lines(self, limit: int = 50) -> list[str]:
        with self._lock:
            return self._log_lines[-limit:]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _log(self, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._lock:
            self._log_lines.append(line)
            if len(self._log_lines) > 1000:
                self._log_lines.pop(0)

    def _set_step(self, step: str) -> None:
        with self._lock:
            self._current_step = step

    def _set_config(self, config: str) -> None:
        with self._lock:
            self._current_config = config

    def _update_sensor(self, sensor_id: str, status: str) -> None:
        with self._lock:
            self._sensor_status[sensor_id] = status

    def _write_event(self, writer, f, event: str, config_file: str = "",
                     purpose: str = "", sensor_id: str = "", details: str = "") -> None:
        ts = datetime.now().isoformat(timespec="seconds")
        writer.writerow([ts, event, config_file, purpose, sensor_id, details])
        f.flush()
        os.fsync(f.fileno())

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------
    def _run_loop(self, settings: dict, sequence: dict, events_file: str) -> None:
        sensor_ids: list[str] = settings["sensor_ids"]
        base_url: str = settings.get("base_url", "https://iot.int.tractian.com")
        user_id: str = settings.get("user_id", "")
        retry_interval: int = int(sequence.get("retry_interval_minutes") or
                                  settings.get("retry_interval_minutes") or 1)
        check_firmware: bool = bool(sequence.get("check_firmware", settings.get("check_firmware", True)))

        client = IoTClient(base_url, user_id)

        try:
            with open(events_file, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Timestamp", "Event", "Config File", "Purpose", "Sensor ID", "Details"])
                f.flush()

                self._write_event(writer, f, "run_started", details=f"sensors={len(sensor_ids)}")
                self._log(f"Run started — {len(sensor_ids)} sensors, {len(sequence['steps'])} steps")

                # Step 1: Firmware check
                if check_firmware:
                    self._set_step("Checking firmware...")
                    ok = self._check_firmware(client, sensor_ids, writer, f, retry_interval)
                    if not ok:
                        self._write_event(writer, f, "run_aborted", details="firmware check failed")
                        return

                # Step 2+: Sequence steps
                steps = sequence.get("steps", [])
                for i, step in enumerate(steps, 1):
                    if self._stop_event.is_set():
                        self._write_event(writer, f, "run_stopped", details=f"step={i}")
                        self._log("Run stopped by user")
                        break

                    config_file = step.get("config_file", "")
                    purpose = step.get("purpose", "")
                    duration_h = float(step.get("duration_hours") or 0)
                    duration_m = float(step.get("duration_minutes") or 0)
                    total_s = duration_h * 3600 + duration_m * 60

                    self._set_step(f"[{i}/{len(steps)}] Sending config: {config_file}")
                    self._set_config(config_file)
                    self._log(f"[{i}/{len(steps)}] Step: {config_file} — {purpose}")

                    # Send config to all sensors
                    revisions = self._send_config(
                        client, sensor_ids, config_file, purpose, writer, f
                    )
                    if revisions is None:
                        self._write_event(writer, f, "run_aborted", details=f"send_config failed step={i}")
                        break

                    # Wait for all sensors to apply config
                    if revisions:
                        self._set_step(f"[{i}/{len(steps)}] Waiting config applied: {config_file}")
                        applied = self._wait_config_applied(
                            client, sensor_ids, revisions, config_file, purpose,
                            writer, f, retry_interval
                        )
                        if not applied:
                            self._write_event(writer, f, "run_aborted", details=f"config_applied failed step={i}")
                            break

                    # Timer
                    if total_s > 0 and not self._stop_event.is_set():
                        label = ""
                        if duration_h:
                            label += f"{duration_h:.0f}h"
                        if duration_m:
                            label += f" {duration_m:.0f}min"
                        label = label.strip()
                        self._set_step(f"[{i}/{len(steps)}] Timer {label}: {config_file}")
                        self._write_event(writer, f, "timer_started", config_file, purpose,
                                          details=label)
                        self._log(f"Timer started: {label}")
                        self._wait_timer(total_s)
                        self._write_event(writer, f, "timer_finished", config_file, purpose,
                                          details=label)
                        self._log(f"Timer finished: {label}")
                else:
                    # All steps completed normally
                    self._write_event(writer, f, "run_completed")
                    self._log("Run completed successfully")
                    self._set_step("Completed")

        except Exception as e:
            self._log(f"FATAL ERROR: {e}")
        finally:
            with self._lock:
                self._running = False
                if not self._current_step or self._current_step == "Starting...":
                    self._current_step = "Stopped"

    def _check_firmware(self, client: IoTClient, sensor_ids: list[str],
                         writer, f, retry_interval: int) -> bool:
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            self._log(f"Checking firmware (attempt {attempt})...")
            all_ok = True
            for sid in sensor_ids:
                status = client.get_last_status(sid)
                if status is None:
                    self._update_sensor(sid, "No response")
                    all_ok = False
                    continue
                fw = status.get("firmwareVersion") or ""
                if fw.startswith(EXPECTED_FW_PREFIX):
                    self._update_sensor(sid, f"FW OK: {fw}")
                else:
                    self._update_sensor(sid, f"FW Wrong: {fw or 'unknown'}")
                    all_ok = False

            if all_ok:
                self._write_event(writer, f, "firmware_ok", details=f"attempt={attempt}")
                self._log(f"All {len(sensor_ids)} sensors on firmware {EXPECTED_FW_PREFIX}*")
                return True

            self._log(f"Waiting {retry_interval} min before retry...")
            self._interruptible_sleep(retry_interval * 60)

        return False

    def _send_config(self, client: IoTClient, sensor_ids: list[str],
                     config_file: str, purpose: str, writer, f) -> dict | None:
        config_path = self._configs_dir() / config_file
        if not config_path.exists():
            self._log(f"ERROR: Config file not found: {config_file}")
            return None
        try:
            template = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            self._log(f"ERROR: Failed to read config {config_file}: {e}")
            return None

        revisions: dict[str, int] = {}
        for sid in sensor_ids:
            if self._stop_event.is_set():
                return None
            self._update_sensor(sid, f"Sending {config_file}...")
            try:
                current = client.get_config(sid)
                cur_cfg = current.get("config") or current
            except Exception as e:
                self._log(f"  ERROR {sid}: Failed to read current config: {e}")
                self._update_sensor(sid, "Error reading config")
                return None

            body = json.loads(json.dumps(template))
            cfg = body.get("config") or body
            cfg["deviceId"] = sid
            cfg["macAddress"] = cur_cfg.get("macAddress", "")
            cfg["idData"] = cur_cfg.get("idData", "")

            try:
                client.post_config(sid, body)
                self._log(f"  OK {sid}: config sent")
            except requests.HTTPError as e:
                self._log(f"  ERROR {sid}: HTTP {e.response.status_code}")
                self._update_sensor(sid, f"Send error: {e.response.status_code}")
                return None
            except Exception as e:
                self._log(f"  ERROR {sid}: {e}")
                self._update_sensor(sid, f"Send error: {e}")
                return None

            try:
                readback = client.get_config(sid)
                rb_cfg = readback.get("config") or readback
                rev = rb_cfg.get("configRevision")
                revisions[sid] = rev
                self._update_sensor(sid, f"Sent (rev {rev})")
                self._write_event(writer, f, "config_sent", config_file, purpose, sid,
                                  details=f"rev={rev}")
                self._log(f"    {sid}: configRevision={rev}")
            except Exception as e:
                self._log(f"  ERROR {sid}: Failed to read back config: {e}")
                self._update_sensor(sid, "Error reading revision")
                return None

        return revisions

    def _wait_config_applied(self, client: IoTClient, sensor_ids: list[str],
                              revisions: dict[str, int], config_file: str, purpose: str,
                              writer, f, retry_interval: int) -> bool:
        attempt = 0
        while not self._stop_event.is_set():
            attempt += 1
            self._log(f"Checking config applied (attempt {attempt})...")
            all_ok = True
            for sid in sensor_ids:
                expected = revisions.get(sid)
                if expected is None:
                    continue
                status = client.get_last_status(sid)
                if status is None:
                    self._update_sensor(sid, "No response (wait)")
                    all_ok = False
                    continue
                actual = status.get("configRevision")
                if actual == expected:
                    self._update_sensor(sid, f"Applied (rev {actual})")
                    self._write_event(writer, f, "config_applied", config_file, purpose, sid,
                                      details=f"configRevision={actual}")
                else:
                    self._update_sensor(sid, f"Waiting rev {expected} (got {actual})")
                    all_ok = False

            if all_ok:
                self._write_event(writer, f, "config_applied_all", config_file, purpose,
                                  details=f"attempt={attempt}")
                self._log("All sensors applied the new config")
                return True

            self._log(f"Waiting {retry_interval} min...")
            self._interruptible_sleep(retry_interval * 60)

        return False

    def _wait_timer(self, total_s: float) -> None:
        elapsed = 0.0
        while elapsed < total_s and not self._stop_event.is_set():
            chunk = min(1.0, total_s - elapsed)
            time.sleep(chunk)
            elapsed += chunk

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop_event.is_set():
            time.sleep(min(1.0, end - time.time()))
