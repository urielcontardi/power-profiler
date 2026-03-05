# Joulescope Logger

App web unificado para captura contínua de dados de consumo de energia via Joulescope, com interface web e visualização em tempo real.

## Funcionalidades

- **Interface web**: Página única com controles de captura e gráficos
- **Captura contínua**: Salva dados em janelas de tempo configuráveis em CSV
- **Visualização em tempo real**: Gráficos de corrente, tensão, potência e energia
- **Docker**: Execução containerizada com acesso USB ao Joulescope

## Estrutura

```
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py              # FastAPI: REST, WebSocket
│       ├── joulescope_manager.py # Captura em background
│       └── static/
│           └── index.html       # Frontend
├── logs/                        # CSVs de experimentos (volume)
├── docker-compose.yml
├── run.sh                       # Roda o app da raiz (./run.sh)
└── README.md
```

## Uso

### Com Docker (recomendado)

```bash
# Criar diretório de logs no SD (Radxa)
sudo mkdir -p /mnt/external_sd/logs
sudo chown -R $USER:$USER /mnt/external_sd/logs

# Subir o container
docker compose up -d --build

# Acessar (porta 8081 no host; no compose: 8081:8080)
# http://localhost:8081
```

### Sem Docker (desenvolvimento)

**Da raiz do projeto** (recomendado):

```bash
# Primeira vez: criar venv e instalar dependências
python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt

# Rodar o app (porta 8080; use PORT=8081 ./run.sh para outra porta)
./run.sh
```

**De dentro de `backend`**:

```bash
cd backend
pip install -r requirements.txt
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

### Sem Docker como serviço (Radxa com systemd)

Para rodar sem manter terminal aberto, configure o app como serviço `systemd`.

1. Criar o serviço (na Radxa, com o repo em `~/power-profiler` e usuário `serial`):

```bash
# Opção A: copiar o arquivo do repositório (ajuste User/Paths se seu usuário ou pasta for outro)
sudo cp ~/power-profiler/scripts/joulescope-logger.service /etc/systemd/system/
# Opção B: criar manualmente (troque 'serial' e 'power-profiler' se necessário)
sudo tee /etc/systemd/system/joulescope-logger.service > /dev/null <<'EOF'
[Unit]
Description=Joulescope Logger (uvicorn)
After=network.target

[Service]
User=serial
WorkingDirectory=/home/serial/power-profiler/backend
Environment=LOG_DIR=/home/serial/power-profiler/logs
ExecStart=/home/serial/power-profiler/backend/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8081
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
```

2. Habilitar no boot e iniciar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now joulescope-logger
```

3. Comandos úteis:

```bash
sudo systemctl status joulescope-logger
sudo systemctl restart joulescope-logger
sudo systemctl stop joulescope-logger
journalctl -u joulescope-logger -f
```

4. Após alterar o arquivo de serviço:

```bash
sudo systemctl daemon-reload
sudo systemctl restart joulescope-logger
```

> **Nota**: O Joulescope precisa estar conectado via USB. No Linux, pode ser necessário adicionar regras udev para acesso ao dispositivo.

## Rodando na Radxa (ARM64 / Linux)

O projeto está pronto para rodar na Radxa:

| Item | Status |
|------|--------|
| **Docker** | Imagem `python:3.11-slim` é multi-arquitetura; no ARM64 a build usa a imagem aarch64. |
| **USB / Joulescope** | `privileged: true`, volume `/dev` e udev; udev no **host** (veja [Linux: regras udev](#linux-regras-udev-obrigatório-para-usb)). |
| **Logs no SD** | Volume em `/mnt/external_sd/logs` no `docker-compose`; crie o dir e permissões antes de subir. |
| **Auto-restart** | `restart: always` + `AUTO_START_CAPTURE=1` para continuar após timeout/re-enumeração USB. |
| **Python (ARM)** | Dependências (numpy, joulescope, etc.) possuem wheels ou build no ARM; primeira build pode demorar um pouco. |

**Checklist na Radxa antes de `docker compose up`:**
1. Instalar regras udev no host: `sudo ./scripts/install-udev-rules.sh 99` (ou `72` se for usuário no console).
2. Se usar `99`: `sudo usermod -a -G plugdev $USER` e novo login.
3. Criar diretório de logs: `sudo mkdir -p /mnt/external_sd/logs && sudo chown -R $USER:$USER /mnt/external_sd/logs`.
4. Conectar o Joulescope e subir: `docker compose up -d --build`. Acessar em **http://&lt;ip-da-radxa&gt;:8081**.

## Configuração

| Variável | Default | Descrição |
|----------|---------|-----------|
| `LOG_DIR` | `/app/logs` | Diretório dos arquivos CSV |
| `PORT` | `8080` | Porta HTTP |
| `TZ` | `America/Sao_Paulo` | Fuso horário |
| `AUTO_START_CAPTURE` | `0` | `1` = inicia captura automaticamente ao subir (e após restart) |
| `AUTO_START_DELAY_SEC` | `5` | Segundos de espera antes de auto-iniciar (USB estabilizar) |
| `AUTO_CAPTURE_FILE` | `joulescope_log.csv` | Arquivo de saída quando auto-inicia |
| `AUTO_CAPTURE_ROTATE_MIN` | `60` | Rotação de arquivo (min) quando auto-inicia |

## Localização dos dados

Os arquivos CSV são salvos em `/mnt/external_sd/logs/` (SD externo na Radxa). Crie o diretório antes de subir:

```bash
sudo mkdir -p /mnt/external_sd/logs
sudo chown -R $USER:$USER /mnt/external_sd/logs
```

## Linux: regras udev (obrigatório para USB)

No **host Linux** (antes de rodar o Docker), instale as regras udev para o Joulescope:

```bash
# Opção 1: systemd (usuário logado no console)
sudo ./scripts/install-udev-rules.sh 72

# Opção 2: grupo plugdev (SSH, sem display)
sudo ./scripts/install-udev-rules.sh 99
sudo usermod -a -G plugdev $USER
# Faça logout e login
```

Reconecte o Joulescope após instalar.

## Logging contínuo (sem intervenção manual)

Quando ocorre timeout do jsdrv, o dispositivo re-enumera e o driver no processo deixa de vê-lo. O container precisa reiniciar para o novo processo enxergar o dispositivo.

Com `AUTO_START_CAPTURE=1` (já configurado no docker-compose):

1. A captura **inicia automaticamente** ao subir o container (após 8 s).
2. Após ~15 falhas consecutivas de "Dispositivo não encontrado", o processo sai e o Docker reinicia o container (`restart: always`).
3. O container sobe de novo e **inicia a captura automaticamente** após 8 s.
4. O novo processo vê o dispositivo re-enumerado e a captura continua.

O intervalo sem log fica em torno de **~20–25 s** (5 falhas × 2 s + restart + 5 s delay), sem precisar clicar em "Iniciar Captura".

## Diagnóstico (Docker)

Para entender se falhas são de **hardware** (cabo/hub), **driver** (pyjoulescope/jsdrv) ou **permissão** (udev):

1. **Na interface web**: use a seção **"Diagnóstico USB (driver / hardware)"** e clique em **"Executar diagnóstico"**. O resultado mostra lsusb, nós em `/dev/bus/usb`, udev e se o driver Python vê o Joulescope, com uma conclusão sugerida.

2. **No terminal (dentro do container)**:
   ```bash
   docker exec joulescope-logger /app/scripts/diagnose.sh
   ```
   Ou acesse a API: `curl http://localhost:8081/api/diagnostics`
