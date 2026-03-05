#!/usr/bin/env bash
# Roda o Joulescope Logger a partir da raiz do projeto (sem precisar cd backend).
# Requer: backend/.venv com dependências instaladas (pip install -r backend/requirements.txt)

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="$ROOT/backend/.venv"
BACKEND="$ROOT/backend"

if [[ ! -d "$VENV" ]]; then
  echo "Ambiente virtual não encontrado. Crie com:"
  echo "  python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

export PYTHONPATH="$BACKEND"
exec "$VENV/bin/python" -m uvicorn app.main:app --reload --host 0.0.0.0 --port "${PORT:-8080}"
