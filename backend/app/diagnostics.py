"""
Diagnóstico USB/Joulescope dentro do container.
Ajuda a distinguir: hardware (cabo/hub), driver (pyjoulescope/jsdrv), permissões (udev).
"""

import logging
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .joulescope_manager import JoulescopeManager

logger = logging.getLogger(__name__)

# Vendor:Product do Joulescope JS220
JOULESCOPE_VID_PID = "16d0:10ba"


def _run(cmd: list[str], timeout: int = 10) -> tuple[str, str, int]:
    """Executa comando; retorna (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "LANG": "C"},
        )
        return (r.stdout or "", r.stderr or "", r.returncode)
    except FileNotFoundError:
        return ("", f"Comando não encontrado: {cmd[0]}", -1)
    except subprocess.TimeoutExpired:
        return ("", "Timeout", -2)


def run_lsusb() -> str:
    """Lista dispositivos USB (lsusb)."""
    out, err, _ = _run(["lsusb"])
    if err and not out:
        return err.strip()
    return out.strip()


def run_lsusb_tree() -> str:
    """Árvore USB (lsusb -t)."""
    out, err, _ = _run(["lsusb", "-t"])
    if err and not out:
        return err.strip()
    return out.strip()


def list_dev_bus_usb() -> list[dict]:
    """Lista nós em /dev/bus/usb com permissões."""
    result = []
    base = "/dev/bus/usb"
    if not os.path.isdir(base):
        return [{"error": f"{base} não existe ou não acessível"}]
    try:
        for bus in sorted(os.listdir(base)):
            bus_path = os.path.join(base, bus)
            if not os.path.isdir(bus_path):
                continue
            for dev in sorted(os.listdir(bus_path)):
                dev_path = os.path.join(bus_path, dev)
                try:
                    st = os.stat(dev_path)
                    result.append({
                        "path": f"{bus}/{dev}",
                        "mode": oct(st.st_mode)[-4:],
                        "uid": st.st_uid,
                        "gid": st.st_gid,
                    })
                except OSError as e:
                    result.append({"path": f"{bus}/{dev}", "error": str(e)})
    except OSError as e:
        return [{"error": str(e)}]
    return result


def run_udevadm_for_joulescope() -> str:
    """Obtém udevadm info para dispositivos 16d0:10ba (via /sys/bus/usb/devices)."""
    lines = []
    sysfs_base = "/sys/bus/usb/devices"
    if not os.path.isdir(sysfs_base):
        return f"{sysfs_base} não acessível"
    try:
        for name in os.listdir(sysfs_base):
            vid_path = os.path.join(sysfs_base, name, "idVendor")
            pid_path = os.path.join(sysfs_base, name, "idProduct")
            dev_path = os.path.join(sysfs_base, name, "dev")
            if not os.path.isfile(vid_path) or not os.path.isfile(pid_path):
                continue
            try:
                with open(vid_path) as f:
                    vid = f.read().strip()
                with open(pid_path) as f:
                    pid = f.read().strip()
                if vid != "16d0" or pid != "10ba":
                    continue
                # Encontrar nó em /dev: dev contém "major:minor", podemos buscar por busnum e devnum
                busnum_path = os.path.join(sysfs_base, name, "busnum")
                devnum_path = os.path.join(sysfs_base, name, "devnum")
                if os.path.isfile(busnum_path) and os.path.isfile(devnum_path):
                    with open(busnum_path) as f:
                        busnum = f.read().strip()
                    with open(devnum_path) as f:
                        devnum = f.read().strip()
                    dev_node = f"/dev/bus/usb/{busnum.zfill(3)}/{devnum.zfill(3)}"
                else:
                    dev_node = f"(sysfs {name})"
                out, err, _ = _run(["udevadm", "info", dev_node])
                lines.append(f"--- {dev_node} ---")
                lines.append(out or err or "(sem saída)")
            except (OSError, ValueError):
                pass
    except OSError as e:
        return str(e)
    return "\n".join(lines) if lines else "Nenhum dispositivo 16d0:10ba em /sys"


def check_lsusb_has_joulescope(lsusb_out: str) -> bool:
    """Verifica se a saída do lsusb contém Joulescope (16d0:10ba)."""
    return "16d0" in lsusb_out and "10ba" in lsusb_out and "Joulescope" in lsusb_out


def collect(manager: "JoulescopeManager") -> dict:
    """
    Coleta diagnóstico completo e tenta classificar a causa.
    Retorna dict com: lsusb, lsusb_tree, dev_bus_usb, udevadm_joulescope,
    driver_sees_device, conclusion, summary.
    """
    lsusb_out = run_lsusb()
    lsusb_tree = run_lsusb_tree()
    dev_nodes = list_dev_bus_usb()
    udev_out = run_udevadm_for_joulescope()

    # Driver Python (joulescope.scan) vê o dispositivo?
    driver_ok = False
    driver_error = None
    try:
        devices = manager.get_devices()
        if devices and not (len(devices) == 1 and "error" in devices[0]):
            driver_ok = True
        elif devices and len(devices) == 1 and "error" in devices[0]:
            driver_error = devices[0].get("error", "Erro desconhecido")
    except Exception as e:
        driver_error = str(e)

    # Conclusão
    hardware_seen = check_lsusb_has_joulescope(lsusb_out)
    summary_parts = []
    conclusion = []

    if not hardware_seen:
        conclusion.append("HARDWARE/CABO/HUB: O Joulescope não aparece no lsusb dentro do container.")
        conclusion.append("Possíveis causas: cabo USB com defeito, hub sem alimentação, dispositivo em outra porta, host não enxerga o dispositivo (ver dmesg no host).")
        summary_parts.append("Dispositivo não visto no USB (lsusb)")
    else:
        summary_parts.append("Dispositivo visto no USB (lsusb)")
        if not driver_ok:
            conclusion.append("DRIVER/PERMISSÃO: O Joulescope aparece no lsusb mas NÃO é visto pela API Python (joulescope.scan).")
            conclusion.append("Possíveis causas: permissão udev (acesso ao dispositivo), driver jsdrv em estado ruim após timeout, container sem acesso ao dispositivo correto.")
            if driver_error:
                conclusion.append(f"Erro do driver: {driver_error}")
            summary_parts.append("não visto pelo driver Python")
        else:
            conclusion.append("OK: Joulescope visível tanto no lsusb quanto na API Python.")
            summary_parts.append("e pelo driver Python")

    return {
        "lsusb": lsusb_out,
        "lsusb_tree": lsusb_tree,
        "dev_bus_usb": dev_nodes,
        "udevadm_joulescope": udev_out,
        "driver_sees_device": driver_ok,
        "driver_error": driver_error,
        "conclusion": conclusion,
        "summary": "; ".join(summary_parts),
    }
