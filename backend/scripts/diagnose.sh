#!/bin/sh
# Diagnóstico USB/Joulescope dentro do container.
# Uso: docker exec joulescope-logger /app/scripts/diagnose.sh
# Ou chamado pela API /api/diagnostics.

echo "=== lsusb (dispositivos USB vistos pelo container) ==="
lsusb 2>/dev/null || echo "lsusb não disponível"

echo ""
echo "=== lsusb -t (árvore USB) ==="
lsusb -t 2>/dev/null || echo "lsusb -t não disponível"

echo ""
echo "=== Dispositivos em /dev/bus/usb (acessíveis ao container) ==="
ls -la /dev/bus/usb/*/* 2>/dev/null || echo "/dev/bus/usb não acessível"

echo ""
echo "=== Joulescope (16d0:10ba) no lsusb ==="
lsusb -d 16d0:10ba 2>/dev/null || echo "Nenhum dispositivo 16d0:10ba encontrado"

echo ""
echo "=== udevadm info para dispositivos Joulescope (se existirem) ==="
for dev in /dev/bus/usb/*/*; do
  if [ -e "$dev" ]; then
    # Ler idVendor e idProduct via sysfs quando disponível
    bus="${dev#/dev/bus/usb/}"
    bus="${bus%/*}"
    devnum="${dev##*/}"
    sysfs="/sys/bus/usb/devices/${bus}-${devnum}"
    if [ -d "$sysfs" ] && [ -f "$sysfs/idVendor" ] && [ -f "$sysfs/idProduct" ]; then
      vid=$(cat "$sysfs/idVendor" 2>/dev/null)
      pid=$(cat "$sysfs/idProduct" 2>/dev/null)
      if [ "$vid" = "16d0" ] && [ "$pid" = "10ba" ]; then
        echo "--- $dev ---"
        udevadm info "$dev" 2>/dev/null || true
      fi
    fi
  fi
done

echo ""
echo "=== Permissões de /dev/bus/usb ==="
ls -la /dev/bus/usb 2>/dev/null || true
