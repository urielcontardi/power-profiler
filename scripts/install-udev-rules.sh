#!/bin/bash
# Instala regras udev do Joulescope no HOST Linux.
# Execute no host (não dentro do Docker) antes de rodar o container.
# Uso: sudo ./install-udev-rules.sh [72|99]
#   72 = systemd/uaccess (usuário logado no console)
#   99 = grupo plugdev (SSH, etc.)

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UDEV_DIR="$(dirname "$SCRIPT_DIR")/udev"

RULE="${1:-72}"
if [ "$RULE" = "72" ]; then
  SRC="$UDEV_DIR/72-joulescope.rules"
elif [ "$RULE" = "99" ]; then
  SRC="$UDEV_DIR/99-joulescope.rules"
else
  echo "Uso: sudo $0 [72|99]"
  echo "  72 = systemd (usuário logado)"
  echo "  99 = grupo plugdev (SSH)"
  exit 1
fi

if [ ! -f "$SRC" ]; then
  echo "Erro: $SRC não encontrado"
  exit 1
fi

cp "$SRC" /etc/udev/rules.d/
udevadm control --reload-rules
echo "Regras udev instaladas. Reconecte o Joulescope se necessário."
if [ "$RULE" = "99" ]; then
  echo "Adicione seu usuário ao grupo plugdev: sudo usermod -a -G plugdev \$USER"
  echo "Faça logout e login para aplicar."
fi
