"""
Joulescope Logger - Web app for continuous power/energy logging.
Single-page frontend with capture controls and visualization.
"""

import asyncio
import logging
import math
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .diagnostics import collect as collect_diagnostics
from .joulescope_manager import JoulescopeManager
from .profiler_manager import ProfilerManager

# Default: ./logs relative to project root (works for local dev); Docker sets LOG_DIR=/app/logs
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LOG_DIR = os.getenv("LOG_DIR", str(_PROJECT_ROOT / "logs"))
PORT = int(os.getenv("PORT", "8080"))

manager = JoulescopeManager(log_dir=LOG_DIR)
profiler = ProfilerManager(log_dir=LOG_DIR)

# Configuração do logger: INFO para ver status e ações do programa
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("joulescope_logger")

app = FastAPI(title="Joulescope Logger")


@app.on_event("startup")
async def startup():
    logger.info("Joulescope Logger iniciado. LOG_DIR=%s", LOG_DIR)
    # Auto-iniciar captura no startup (útil após restart do container por timeout jsdrv)
    if os.getenv("AUTO_START_CAPTURE", "").lower() in ("1", "true", "yes"):
        delay = float(os.getenv("AUTO_START_DELAY_SEC", "5"))
        output_file = os.getenv("AUTO_CAPTURE_FILE", "joulescope_log.csv")
        rotate_min = float(os.getenv("AUTO_CAPTURE_ROTATE_MIN", "60"))

        async def delayed_auto_start():
            await asyncio.sleep(delay)
            try:
                result = manager.start_capture(
                    window_duration=0,
                    output_file=output_file,
                    rotate_interval_minutes=rotate_min,
                )
                if "error" in result:
                    logger.warning("Auto-captura: %s", result.get("error"))
                else:
                    logger.info("Auto-captura iniciada: %s", output_file)
            except Exception as e:
                logger.warning("Falha ao iniciar auto-captura: %s", e)

        asyncio.create_task(delayed_auto_start())


# --- REST API ---


@app.get("/api/devices")
async def list_devices():
    """List available Joulescope devices."""
    return {"devices": manager.get_devices()}


def _json_safe(obj):
    """Convert nan/inf to None for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (int, str, bool, type(None))):
        return obj
    try:
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return obj


@app.get("/api/capture/status")
async def capture_status():
    """Get current capture status."""
    return _json_safe(manager.get_status())


@app.get("/api/logs")
async def capture_logs(limit: int = 200):
    """Get recent capture/application events for frontend log panel."""
    safe_limit = max(1, min(limit, 1000))
    return {"events": manager.get_events(safe_limit)}


@app.get("/api/diagnostics")
async def get_diagnostics():
    """
    Diagnóstico USB/Joulescope dentro do container.
    Ajuda a distinguir: hardware (cabo/hub), driver (pyjoulescope), permissões (udev).
    """
    try:
        return collect_diagnostics(manager)
    except Exception as e:
        logger.exception("Erro ao coletar diagnóstico")
        return {
            "error": str(e),
            "summary": "Falha ao executar diagnóstico",
            "conclusion": ["Erro ao rodar diagnóstico. Veja os logs do container."],
        }


class CaptureStartRequest(BaseModel):
    window_duration: float = 0.0
    output_file: str = "joulescope_log.csv"
    sampling_rate: float | None = None
    max_windows: int = 0
    rotate_interval_minutes: float = 60.0  # 0 = sem rotação, novo arquivo a cada N min


@app.post("/api/capture/start")
async def capture_start(body: CaptureStartRequest):
    """Start continuous capture."""
    logger.info("API: Iniciando captura (janela=%.1fs, arquivo=%s)", body.window_duration, body.output_file)
    try:
        devices = manager.get_devices()
        has_error = devices and isinstance(devices[0], dict) and "error" in devices[0]
        has_devices = devices and not has_error and len(devices) > 0
        if not has_devices:
            err_msg = devices[0].get("error", "Nenhum dispositivo Joulescope encontrado") if has_error else "Nenhum dispositivo Joulescope encontrado. Conecte o dispositivo via USB."
            return JSONResponse(status_code=400, content={"error": err_msg})
        result = manager.start_capture(
            window_duration=body.window_duration,
            output_file=body.output_file,
            sampling_rate=body.sampling_rate,
            max_windows=body.max_windows,
            rotate_interval_minutes=body.rotate_interval_minutes,
        )
        if "error" in result:
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/capture/stop")
async def capture_stop():
    """Stop current capture."""
    logger.info("API: Parando captura")
    return manager.stop_capture()


@app.get("/api/experiments")
async def list_experiments():
    """List available experiment CSV files."""
    log_path = Path(LOG_DIR)
    if not log_path.exists():
        return {"files": []}
    files = []
    for f in sorted(log_path.glob("*.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
        if "event_" not in f.name:
            files.append({"name": f.name, "path": f.name})
    return {"files": files}


def load_experiment_data(filepath: Path) -> pd.DataFrame | None:
    """Load experiment CSV file."""
    try:
        df = pd.read_csv(filepath)
        if "Window Start" in df.columns:
            df["Window Start"] = pd.to_datetime(df["Window Start"])
        if "Window End" in df.columns:
            df["Window End"] = pd.to_datetime(df["Window End"])
        numeric_cols = [
            "Duration (s)", "Samples",
            "Current Mean (A)", "Current Std (A)", "Current Min (A)", "Current Max (A)",
            "Voltage Mean (V)", "Voltage Std (V)", "Voltage Min (V)", "Voltage Max (V)",
            "Power Mean (W)", "Power Std (W)", "Power Min (W)", "Power Max (W)",
            "Energy (J)", "Energy (mWh)", "Cumulative Energy (J)", "Cumulative Energy (mWh)",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception:
        return None


def create_plots(df: pd.DataFrame) -> dict:
    """Create Plotly figures for experiment data."""
    figures = {}
    if df is None or len(df) == 0:
        return figures

    # Time series: Current, Voltage, Power
    fig1 = make_subplots(
        rows=3, cols=1,
        subplot_titles=("Current (A)", "Voltage (V)", "Power (W)"),
        vertical_spacing=0.08,
        shared_xaxes=True,
    )
    # Linha contínua suave, sem markers, conectando gaps (NaN)
    line_config = dict(shape="spline")
    if "Window Start" in df.columns and "Current Mean (A)" in df.columns:
        x = df["Window Start"].tolist()
        fig1.add_trace(
            go.Scatter(x=x, y=df["Current Mean (A)"].tolist(), mode="lines",
                       name="Current Mean", connectgaps=True,
                       line=dict(color="#58a6ff", **line_config)),
            row=1, col=1,
        )
    if "Window Start" in df.columns and "Voltage Mean (V)" in df.columns:
        x = df["Window Start"].tolist()
        fig1.add_trace(
            go.Scatter(x=x, y=df["Voltage Mean (V)"].tolist(), mode="lines",
                       name="Voltage Mean", connectgaps=True,
                       line=dict(color="#3fb950", **line_config)),
            row=2, col=1,
        )
    if "Window Start" in df.columns and "Power Mean (W)" in df.columns:
        x = df["Window Start"].tolist()
        fig1.add_trace(
            go.Scatter(x=x, y=df["Power Mean (W)"].tolist(), mode="lines",
                       name="Power Mean", connectgaps=True,
                       line=dict(color="#f85149", **line_config)),
            row=3, col=1,
        )
    fig1.update_xaxes(title_text="Time", row=3, col=1)
    # Range selector e slider para selecionar intervalo de tempo
    range_selector = dict(
        buttons=list([
            dict(count=1, label="1h", step="hour", stepmode="todate"),
            dict(count=6, label="6h", step="hour", stepmode="todate"),
            dict(count=1, label="1d", step="day", stepmode="todate"),
            dict(count=7, label="1sem", step="day", stepmode="todate"),
            dict(step="all", label="Tudo"),
        ]),
        x=0, xanchor="left", y=1.15, yanchor="top",
    )
    fig1.update_layout(
        height=500, title_text="Current, Voltage, Power Over Time",
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        uirevision="time_series",
        xaxis3=dict(
            rangeslider=dict(visible=True, thickness=0.05),
            rangeselector=range_selector,
        ),
    )
    figures["time_series"] = fig1.to_json()

    # Energy
    if "Window Start" in df.columns and "Cumulative Energy (J)" in df.columns:
        fig2 = go.Figure()
        x = df["Window Start"].tolist()
        fig2.add_trace(go.Scatter(
            x=x, y=df["Cumulative Energy (J)"].tolist(),
            mode="lines", name="Cumulative Energy", connectgaps=True,
            line=dict(color="#a371f7", width=2, shape="spline"),
        ))
        if "Energy (J)" in df.columns:
            fig2.add_trace(go.Bar(
                x=x, y=df["Energy (J)"].tolist(),
                name="Energy per Window", marker_color="#f0883e", opacity=0.6,
            ))
        fig2.update_layout(
            title="Energy Consumption",
            xaxis_title="Time", yaxis_title="Energy (J)", height=350,
            template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            uirevision="energy",
            xaxis=dict(
                rangeslider=dict(visible=True, thickness=0.05),
                rangeselector=range_selector,
            ),
        )
        figures["energy"] = fig2.to_json()

    return figures


@app.get("/api/download/{filename}")
async def download_experiment(filename: str):
    """Download experiment CSV file."""
    # Sanitize: only allow filename, no path traversal
    safe_name = Path(filename).name
    if not safe_name.endswith(".csv"):
        return JSONResponse(status_code=400, content={"error": "Invalid file"})
    path = Path(LOG_DIR) / safe_name
    if not path.exists() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "File not found"})
    return FileResponse(
        path=path,
        filename=safe_name,
        media_type="text/csv",
    )


@app.delete("/api/experiment/{filename}")
async def delete_experiment(filename: str):
    """Delete an experiment CSV file."""
    safe_name = Path(filename).name
    if not safe_name.endswith(".csv"):
        return JSONResponse(status_code=400, content={"error": "Invalid file"})

    status = manager.get_status()
    active_files = status.get("output_files") or []
    active_names = {Path(str(p)).name for p in active_files}
    current_output = status.get("output_file")
    if current_output:
        active_names.add(Path(str(current_output)).name)

    if status.get("running") and safe_name in active_names:
        return JSONResponse(
            status_code=409,
            content={"error": "Não é possível deletar o CSV que está em captura ativa"},
        )

    path = Path(LOG_DIR) / safe_name
    if not path.exists() or not path.is_file():
        return JSONResponse(status_code=404, content={"error": "File not found"})

    try:
        path.unlink()
        logger.info("CSV deletado: %s", safe_name)
        return {"success": True, "deleted": safe_name}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/experiment/{filename}")
async def get_experiment(filename: str):
    """Get experiment data and plots."""
    try:
        path = Path(LOG_DIR) / filename
        if not path.exists():
            status = manager.get_status()
            out_file = status.get("output_file") or ""
            out_name = Path(out_file).name if out_file else ""
            if status.get("running") and (out_name == path.name or path.name in out_file):
                return {
                    "stats": {
                        "total_windows": 0, "total_energy": "0", "total_energy_mwh": "0",
                        "avg_current": "N/A", "avg_voltage": "N/A", "avg_power": "N/A",
                        "duration": "0h 0m 0s",
                    },
                    "plots": {},
                }
            return JSONResponse(status_code=404, content={"error": "File not found"})

        df = load_experiment_data(path)
        if df is None:
            return JSONResponse(status_code=400, content={"error": "Failed to load file"})

        if len(df) == 0:
            stats = {
                "total_windows": 0, "total_energy": "0", "total_energy_mwh": "0",
                "avg_current": "N/A", "avg_voltage": "N/A", "avg_power": "N/A",
                "duration": "N/A",
            }
        else:
            stats = {
                "total_windows": len(df),
                "total_energy": f"{df['Cumulative Energy (J)'].iloc[-1]:.6f}" if "Cumulative Energy (J)" in df.columns else "N/A",
                "total_energy_mwh": f"{df['Cumulative Energy (mWh)'].iloc[-1]:.6f}" if "Cumulative Energy (mWh)" in df.columns else "N/A",
                "avg_current": f"{df['Current Mean (A)'].mean():.6f}" if "Current Mean (A)" in df.columns else "N/A",
                "avg_voltage": f"{df['Voltage Mean (V)'].mean():.6f}" if "Voltage Mean (V)" in df.columns else "N/A",
                "avg_power": f"{df['Power Mean (W)'].mean():.6f}" if "Power Mean (W)" in df.columns else "N/A",
            }
            if "Window Start" in df.columns and "Window End" in df.columns:
                dur = (df["Window End"].iloc[-1] - df["Window Start"].iloc[0]).total_seconds()
                h, m = int(dur // 3600), int((dur % 3600) // 60)
                stats["duration"] = f"{h}h {m}m"
            else:
                stats["duration"] = "N/A"

        plots = create_plots(df)
        return {"stats": stats, "plots": plots}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


# --- WebSocket for live updates ---


@app.websocket("/api/ws/capture")
async def websocket_capture(websocket: WebSocket):
    """WebSocket for live capture updates."""
    await websocket.accept()
    import asyncio
    queue = asyncio.Queue(maxsize=100)
    loop = asyncio.get_running_loop()

    def on_window(data: dict):
        try:
            loop.call_soon_threadsafe(queue.put_nowait, data)
        except asyncio.QueueFull:
            pass

    manager.subscribe(on_window)
    try:
        while True:
            data = await queue.get()
            await websocket.send_json(data)
    except WebSocketDisconnect:
        pass
    finally:
        manager.unsubscribe(on_window)


# --- Profiler API ---


@app.get("/api/profiler/settings")
async def profiler_get_settings():
    return profiler.get_settings()


@app.post("/api/profiler/settings")
async def profiler_save_settings(body: dict):
    return profiler.save_settings(body)


@app.get("/api/profiler/configs")
async def profiler_list_configs():
    return {"configs": profiler.list_configs()}


@app.post("/api/profiler/configs")
async def profiler_upload_config(file: UploadFile = File(...)):
    content = await file.read()
    return profiler.save_config(file.filename or "config.json", content)


@app.get("/api/profiler/configs/{name}")
async def profiler_get_config(name: str):
    content = profiler.get_config_content(name)
    if content is None:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    return content


@app.delete("/api/profiler/configs/{name}")
async def profiler_delete_config(name: str):
    result = profiler.delete_config(name)
    if "error" in result:
        return JSONResponse(status_code=404, content=result)
    return result


@app.get("/api/profiler/sequence")
async def profiler_get_sequence():
    return profiler.get_sequence()


@app.post("/api/profiler/sequence")
async def profiler_save_sequence(body: dict):
    return profiler.save_sequence(body)


@app.post("/api/profiler/run/start")
async def profiler_start_run():
    result = profiler.start_run()
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/profiler/run/stop")
async def profiler_stop_run():
    return profiler.stop_run()


@app.get("/api/profiler/run/status")
async def profiler_run_status(log_lines: int = 50):
    status = profiler.get_status()
    status["log_lines"] = profiler.get_log_lines(limit=log_lines)
    return status


@app.get("/api/profiler/events")
async def profiler_list_events():
    log_path = Path(LOG_DIR)
    if not log_path.exists():
        return {"files": []}
    files = []
    for f in sorted(log_path.glob("events_*.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
        files.append({"name": f.name, "size": f.stat().st_size})
    return {"files": files}


# --- Static frontend (must be last) ---

static_dir = Path(__file__).parent / "static"


@app.get("/")
async def serve_index():
    """Serve the SPA at root so GET / always returns the frontend (avoids 404 on deploy)."""
    index = static_dir / "index.html"
    if index.exists():
        return FileResponse(index, media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "Frontend index.html not found"})


if static_dir.exists():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
