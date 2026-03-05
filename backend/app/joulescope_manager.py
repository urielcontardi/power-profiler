"""
Joulescope Manager - Background capture service with window-based processing.
Runs continuously when started, saves data to CSV, notifies subscribers for live updates.
Timestamps em horário de São Paulo.

Arquitetura: o manager roda uma thread monitor que cria um subprocesso para cada sessão
de captura. Quando o subprocesso falha (timeout USB, re-enumeração), é encerrado e
substituído por um novo (~2s). O novo processo tem estado jsdrv limpo e enxerga o
dispositivo re-enumerado imediatamente, sem precisar reiniciar o Docker.
"""

import csv
import logging
import multiprocessing
import os
import shutil
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np

logger = logging.getLogger(__name__)

TZ_SAO_PAULO = ZoneInfo("America/Sao_Paulo")
ROTATE_INTERVAL_MINUTES = 60
CONTINUOUS_WINDOW_SEC = 2.0  # Janela quando UI usa 0 (2s = grava mais cedo, não perde ao parar rápido)
EVENT_BUFFER_SIZE = 500
RESPAWN_DELAY_SEC = 2  # Espera antes de criar novo subprocesso após falha


# ---------------------------------------------------------------------------
# Worker function (module-level para ser picklável pelo multiprocessing)
# ---------------------------------------------------------------------------

def _capture_worker(
    result_queue: "multiprocessing.Queue[dict]",
    stop_event: "multiprocessing.Event",
    window_duration: float,
    initial_sampling_rate: float,
) -> None:
    """
    Roda em subprocesso isolado. Conecta uma vez, captura até falhar ou parar.
    SEM retry interno: qualquer erro encerra o processo.
    O manager cria um novo processo, que tem estado jsdrv limpo.
    """
    try:
        import joulescope  # importado no subprocesso para estado limpo
        import numpy as _np

        def _stats(data: "_np.ndarray") -> dict:
            cur = data[:, 0]
            vol = data[:, 1]
            pwr = cur * vol
            return {
                "samples": len(data),
                "current_mean": float(_np.mean(cur, dtype=_np.float64)),
                "current_std": float(_np.std(cur, dtype=_np.float64)),
                "current_min": float(_np.min(cur)),
                "current_max": float(_np.max(cur)),
                "voltage_mean": float(_np.mean(vol, dtype=_np.float64)),
                "voltage_std": float(_np.std(vol, dtype=_np.float64)),
                "voltage_min": float(_np.min(vol)),
                "voltage_max": float(_np.max(vol)),
                "power_mean": float(_np.mean(pwr, dtype=_np.float64)),
                "power_std": float(_np.std(pwr, dtype=_np.float64)),
                "power_min": float(_np.min(pwr)),
                "power_max": float(_np.max(pwr)),
            }

        def _energy(data: "_np.ndarray", sr: float) -> tuple:
            pwr = data[:, 0] * data[:, 1]
            ej = float(_np.sum(pwr) / sr)
            return ej, ej * (1000.0 / 3600.0)

        # --- Scan ---
        devices = joulescope.scan()
        if not devices:
            result_queue.put({"type": "not_found"})
            return

        device = joulescope.scan_require_one(config="auto")
        result_queue.put({"type": "connected", "device": str(device)})

        with device:
            sr = initial_sampling_rate or 1_000_000.0

            try:
                device.parameter_set("buffer_duration", max(4.0, window_duration * 2 + 1))
            except Exception:
                pass

            device.start()
            time.sleep(0.5)

            # Evitar probe de leitura aqui: em alguns cenários do JS220 essa leitura
            # curta também pode bloquear. Mantemos a taxa informada (ou fallback padrão)
            # e seguimos diretamente para o loop de captura.
            result_queue.put({"type": "sampling_rate", "rate": sr})

            # Loop de captura: acumula chunks de 0.1 s e emite janela por wall-clock.
            # Para evitar bloqueio infinito no read(), usa timeout quando disponível.
            CHUNK_SEC = 0.1
            accumulated: list = []
            t_win_start = time.time()
            import inspect as _inspect
            _read_sig = _inspect.signature(device.read)
            _read_supports_timeout = "timeout" in _read_sig.parameters

            while not stop_event.is_set():
                if _read_supports_timeout:
                    chunk = device.read(duration=CHUNK_SEC, timeout=2.0)
                else:
                    chunk = device.read(duration=CHUNK_SEC)

                if chunk is None or len(chunk) == 0:
                    result_queue.put({"type": "read_empty", "window_sec": CHUNK_SEC})
                    time.sleep(0.01)
                    continue

                accumulated.append(chunk)

                if time.time() - t_win_start >= window_duration:
                    t_end = time.time()
                    data = _np.concatenate(accumulated, axis=0)
                    actual_dur = t_end - t_win_start
                    n = len(data)
                    expected = int(sr * actual_dur)
                    tol = max(10000, int(sr * 0.05))
                    gap = n < (expected - tol)

                    stats = _stats(data)
                    ej, emwh = _energy(data, sr)

                    result_queue.put({
                        "type": "window",
                        "t_start": t_win_start,
                        "t_end": t_end,
                        "actual_duration": actual_dur,
                        "stats": stats,
                        "energy_joules": ej,
                        "energy_mwh": emwh,
                        "sampling_rate": sr,
                        "gap": gap,
                        "samples": n,
                    })

                    accumulated = []
                    t_win_start = time.time()

            try:
                device.stop()
            except Exception:
                pass

    except Exception as e:
        import traceback as _tb
        result_queue.put({"type": "error", "msg": str(e), "tb": _tb.format_exc()})


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class JoulescopeManager:
    """Gerencia a captura. Usa subprocesso para o joulescope; reinicia sem Docker restart."""

    CSV_HEADERS = [
        'Timestamp', 'Window Start', 'Window End', 'Duration (s)', 'Samples',
        'Current Mean (A)', 'Current Std (A)', 'Current Min (A)', 'Current Max (A)',
        'Voltage Mean (V)', 'Voltage Std (V)', 'Voltage Min (V)', 'Voltage Max (V)',
        'Power Mean (W)', 'Power Std (W)', 'Power Min (W)', 'Power Max (W)',
        'Energy (J)', 'Energy (mWh)', 'Cumulative Energy (J)', 'Cumulative Energy (mWh)',
        'Data Gap Warning'
    ]

    def __init__(self, log_dir: str):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._running = False
        self._capture_thread: Optional[threading.Thread] = None
        self._subscribers: list[Callable] = []
        self._status = {
            'running': False,
            'output_file': None,
            'output_files': [],
            'start_time': None,
            'window_count': 0,
            'total_energy': 0.0,
            'last_window': None,
            'reconnect_count': 0,
            'last_error': None,
        }
        self._events: deque[dict] = deque(maxlen=EVENT_BUFFER_SIZE)
        self._push_event("info", "Joulescope manager inicializado")

    def _push_event(self, level: str, message: str):
        event = {
            "timestamp": self._now_sp().isoformat(),
            "level": level.upper(),
            "message": message,
        }
        with self._lock:
            self._events.append(event)

    def get_events(self, limit: int = 200) -> list[dict]:
        with self._lock:
            if limit <= 0:
                return []
            return list(self._events)[-limit:]

    def subscribe(self, callback: Callable):
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable):
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def _notify(self, data: dict):
        with self._lock:
            callbacks = list(self._subscribers)
        for cb in callbacks:
            try:
                cb(data)
            except Exception:
                pass

    def get_status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def get_devices(self) -> list[dict]:
        """Scan rápido para verificar dispositivo. Não usa subprocesso."""
        try:
            import joulescope
            devices = joulescope.scan()
            return [{'id': str(d), 'name': str(d)} for d in devices]
        except Exception as e:
            logger.warning("Erro ao escanear dispositivos: %s", e)
            self._push_event("warning", f"Erro ao escanear dispositivos: {e}")
            return [{'error': str(e)}]

    def _now_sp(self) -> datetime:
        return datetime.now(TZ_SAO_PAULO)

    def _initialize_csv(self, csv_path: Path):
        if not csv_path.exists():
            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                csv.writer(f).writerow(self.CSV_HEADERS)
        else:
            try:
                with open(csv_path, 'r', encoding='utf-8') as f:
                    existing = next(csv.reader(f), None)
                    if existing is None or len(existing) != len(self.CSV_HEADERS):
                        backup = csv_path.with_suffix('.csv.backup')
                        if csv_path.stat().st_size > 0:
                            shutil.copy2(csv_path, backup)
                        with open(csv_path, 'w', newline='', encoding='utf-8') as fw:
                            csv.writer(fw).writerow(self.CSV_HEADERS)
            except Exception:
                pass

    def _log_to_csv(self, csv_path: Path, window_start: datetime, window_end: datetime,
                    duration: float, stats: dict, energy_joules: float, energy_mwh: float,
                    total_energy: float, gap_detected: bool):
        total_mwh = total_energy * (1000.0 / 3600.0)
        now = self._now_sp()
        row = [
            now.strftime('%Y-%m-%d %H:%M:%S.%f'),
            window_start.strftime('%Y-%m-%d %H:%M:%S.%f'),
            window_end.strftime('%Y-%m-%d %H:%M:%S.%f'),
            f'{duration:.6f}', stats['samples'],
            f'{stats["current_mean"]:.12f}', f'{stats["current_std"]:.12f}',
            f'{stats["current_min"]:.12f}', f'{stats["current_max"]:.12f}',
            f'{stats["voltage_mean"]:.9f}', f'{stats["voltage_std"]:.9f}',
            f'{stats["voltage_min"]:.9f}', f'{stats["voltage_max"]:.9f}',
            f'{stats["power_mean"]:.12f}', f'{stats["power_std"]:.12f}',
            f'{stats["power_min"]:.12f}', f'{stats["power_max"]:.12f}',
            f'{energy_joules:.12f}', f'{energy_mwh:.12f}',
            f'{total_energy:.12f}', f'{total_mwh:.12f}',
            'GAP' if gap_detected else ''
        ]
        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
            csv.writer(f).writerow(row)
            f.flush()
            os.fsync(f.fileno())

    def _get_rotated_path(self, base_name: str) -> Path:
        stem = Path(base_name).stem
        suffix = Path(base_name).suffix or '.csv'
        ts = self._now_sp().strftime('%Y%m%d_%H%M%S')
        return self.log_dir / f"{stem}_{ts}{suffix}"

    def _capture_loop(self, window_duration: float, output_file: str,
                      sampling_rate: Optional[float], max_windows: int,
                      rotate_interval_minutes: float = 0):
        """
        Thread monitor: cria subprocesso worker, processa resultados, reinicia ao falhar.
        SEM lógica de retry no worker — a recuperação é feita pelo spawn de novo processo.
        """
        self.log_dir.mkdir(parents=True, exist_ok=True)
        base_name = Path(output_file).name
        csv_path = self.log_dir / base_name
        self._initialize_csv(csv_path)

        total_energy = 0.0
        window_num = 0
        eff_window = window_duration if window_duration > 0 else CONTINUOUS_WINDOW_SEC
        eff_sr = sampling_rate or 0.0
        last_rotate_time = self._now_sp()
        active_files: list[str] = [str(csv_path)]
        spawn_count = 0

        with self._lock:
            self._status['output_files'] = active_files
            self._status['start_time'] = self._now_sp().isoformat()

        while self._running:
            # Pequeno delay antes de respawn (não no primeiro spawn)
            if spawn_count > 0:
                for _ in range(RESPAWN_DELAY_SEC):
                    if not self._running:
                        break
                    time.sleep(1)
                if not self._running:
                    break

            # Importante para Linux: usar spawn evita problemas de fork com libusb/jsdrv.
            # Com fork, o scan/connect pode funcionar, mas o primeiro read() pode travar.
            mp_ctx = multiprocessing.get_context("spawn")
            result_queue: multiprocessing.Queue = mp_ctx.Queue()
            stop_event: multiprocessing.Event = mp_ctx.Event()

            p = mp_ctx.Process(
                target=_capture_worker,
                args=(result_queue, stop_event, eff_window, eff_sr),
                daemon=True,
            )
            p.start()
            spawn_count += 1
            logger.info("Subprocesso de captura iniciado (pid=%d, spawn=%d)", p.pid, spawn_count)
            self._push_event("info", f"Processo de captura iniciado (pid={p.pid})")
            last_msg_ts = time.time()

            # Drena a fila enquanto o subprocesso viver
            while self._running:
                try:
                    msg = result_queue.get(timeout=1.0)
                except Exception:  # queue.Empty
                    if p.is_alive():
                        # Watchdog: se o worker ficar vivo sem enviar eventos por muito tempo,
                        # considera travado em read() e força respawn.
                        idle_sec = time.time() - last_msg_ts
                        max_idle = max(15.0, eff_window * 5.0)
                        if idle_sec > max_idle:
                            self._push_event("warning", "Sem dados do worker por muito tempo. Reiniciando processo...")
                            break
                    if not p.is_alive():
                        self._push_event("warning", "Processo de captura encerrou inesperadamente. Reiniciando...")
                        break
                    continue

                last_msg_ts = time.time()
                mtype = msg.get("type")

                if mtype == "not_found":
                    self._push_event("warning", "Dispositivo não encontrado. Reiniciando processo em 2s...")
                    with self._lock:
                        self._status['last_error'] = 'Dispositivo não encontrado'
                        self._status['reconnect_count'] = self._status.get('reconnect_count', 0) + 1
                    break

                elif mtype == "connected":
                    logger.info("Joulescope conectado: %s", msg.get("device"))
                    self._push_event("info", f"Joulescope conectado: {msg.get('device')}")
                    with self._lock:
                        self._status['last_error'] = None

                elif mtype == "sampling_rate":
                    eff_sr = msg["rate"]
                    with self._lock:
                        self._status['sampling_rate'] = eff_sr
                    logger.info("Taxa de amostragem detectada: %.0f Hz", eff_sr)

                elif mtype == "read_empty":
                    logger.warning("device.read() retornou vazio (janela=%.1fs). Verifique cabo/USB.", msg.get("window_sec", 0))
                    self._push_event("warning", "Leitura vazia do Joulescope — verifique cabo/USB ou driver.")

                elif mtype == "window":
                    window_num += 1
                    ws = datetime.fromtimestamp(msg["t_start"], tz=TZ_SAO_PAULO)
                    we = datetime.fromtimestamp(msg["t_end"], tz=TZ_SAO_PAULO)
                    ej = msg["energy_joules"]
                    emwh = msg["energy_mwh"]
                    total_energy += ej

                    # Rotação de arquivo
                    if rotate_interval_minutes > 0:
                        elapsed_min = (we - last_rotate_time).total_seconds() / 60.0
                        if elapsed_min >= rotate_interval_minutes:
                            csv_path = self._get_rotated_path(base_name)
                            self._initialize_csv(csv_path)
                            last_rotate_time = we
                            active_files.append(str(csv_path))
                            with self._lock:
                                self._status['output_file'] = str(csv_path)
                                self._status['output_files'] = list(active_files)
                            self._push_event("info", f"Rotação CSV: {csv_path.name}")

                    try:
                        self._log_to_csv(csv_path, ws, we, msg["actual_duration"],
                                         msg["stats"], ej, emwh, total_energy, msg["gap"])
                    except Exception as ex:
                        logger.error("Falha ao gravar CSV: %s", ex)
                        self._push_event("error", f"Falha CSV: {ex}")

                    if msg["gap"]:
                        sr_for_log = msg.get("sampling_rate", eff_sr)
                        expected = int(sr_for_log * msg["actual_duration"])
                        self._push_event("warning",
                            f"Data gap janela {window_num} (esperado ~{expected}, obtido {msg['samples']})")

                    window_data = {
                        'window_num': window_num,
                        'window_start': ws.isoformat(),
                        'window_end': we.isoformat(),
                        'duration': msg["actual_duration"],
                        'stats': msg["stats"],
                        'energy_joules': ej,
                        'energy_mwh': emwh,
                        'total_energy': total_energy,
                        'total_energy_mwh': total_energy * (1000.0 / 3600.0),
                        'samples': msg["samples"],
                    }
                    with self._lock:
                        self._status['window_count'] = window_num
                        self._status['total_energy'] = total_energy
                        self._status['last_window'] = window_data
                        self._status['output_file'] = str(csv_path)
                        self._status['output_files'] = list(active_files)
                    self._notify(window_data)

                    if max_windows > 0 and window_num >= max_windows:
                        logger.info("Limite de janelas atingido (%d).", max_windows)
                        self._push_event("info", f"Limite de janelas atingido: {max_windows}")
                        stop_event.set()
                        break

                elif mtype == "error":
                    logger.warning("Erro no subprocesso: %s", msg.get("msg"))
                    self._push_event("warning", f"Erro: {msg.get('msg', '?')}")
                    if msg.get("tb"):
                        lines = msg["tb"].strip().splitlines()
                        short_tb = " | ".join(lines[-3:]) if len(lines) >= 3 else " | ".join(lines)
                        self._push_event("info", f"Detalhe: {short_tb[:300]}")
                    with self._lock:
                        self._status['last_error'] = msg.get("msg")
                        self._status['reconnect_count'] = self._status.get('reconnect_count', 0) + 1
                    break

            # Encerrar subprocesso (aguardar tempo suficiente para a última janela ser enviada)
            stop_event.set()
            join_timeout = max(5, eff_window + 3)
            p.join(timeout=join_timeout)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)

            if max_windows > 0 and window_num >= max_windows:
                break  # Captura concluída

        with self._lock:
            self._status['running'] = False
            self._status['output_file'] = str(csv_path)
            self._status['output_files'] = active_files
        logger.info("Captura encerrada. Total: %d janelas, %.4f J", window_num, total_energy)
        self._push_event("info", f"Captura encerrada. Janelas={window_num}, energia={total_energy:.4f}J")

    def start_capture(self, window_duration: float = 10.0, output_file: str = 'joulescope_log.csv',
                     sampling_rate: Optional[float] = None, max_windows: int = 0,
                     rotate_interval_minutes: float = ROTATE_INTERVAL_MINUTES) -> dict:
        # Sempre incluir data (YYYYMMDD) no nome do CSV
        base = Path(output_file).stem
        suffix = Path(output_file).suffix or ".csv"
        if suffix.lower() != ".csv":
            suffix = ".csv"
        date_str = datetime.now(TZ_SAO_PAULO).strftime("%Y%m%d")
        output_file = f"{base}_{date_str}{suffix}"

        with self._lock:
            if self._status['running']:
                self._push_event("warning", "Captura já em andamento. Start ignorado")
                return {'error': 'Capture already running'}
            self._running = True
            self._status = {
                'running': True,
                'output_file': output_file,
                'output_files': [],
                'start_time': None,
                'window_count': 0,
                'total_energy': 0.0,
                'last_window': None,
                'reconnect_count': 0,
                'last_error': None,
            }

        def run():
            try:
                self._capture_loop(window_duration, output_file, sampling_rate,
                                   max_windows, rotate_interval_minutes)
            except BaseException as e:
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                import traceback
                logger.error("Captura encerrada por exceção: %s\n%s", e, traceback.format_exc())
                self._push_event("error", f"Captura parou inesperadamente: {e}")
                with self._lock:
                    self._status['running'] = False
                    self._status['last_error'] = str(e)

        self._capture_thread = threading.Thread(target=run, daemon=True)
        self._capture_thread.start()
        logger.info("Captura iniciada: %s (janela=%.1fs)", output_file, window_duration)
        self._push_event("info", f"Captura iniciada: {output_file}")
        return {'success': True, 'output_file': output_file}

    def stop_capture(self) -> dict:
        logger.info("Parando captura...")
        self._push_event("info", "Parando captura...")
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=15)
            self._capture_thread = None
        with self._lock:
            self._status['running'] = False
        logger.info("Captura parada.")
        self._push_event("info", "Captura parada")
        return {'success': True}
