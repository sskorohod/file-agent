# Qdrant Setup on UGreen Server

## Quick Start

```bash
# 1. SSH into the server
ssh ugreen

# 2. Create directory and copy files
mkdir -p ~/qdrant && cd ~/qdrant

# 3. Copy docker-compose and config (from your Mac)
# Run from project root on Mac:
scp infra/docker-compose.qdrant.yml ugreen:~/qdrant/docker-compose.yml
scp infra/qdrant_config.yaml ugreen:~/qdrant/qdrant_config.yaml

# 4. Start Qdrant (on ugreen)
cd ~/qdrant
docker compose up -d

# 5. Verify
curl http://localhost:6333/healthz
# Should return: {"title":"qdrant - vectorass engine","version":"..."}
```

## Verify from Mac

```bash
# Replace UGREEN_IP with actual IP or hostname
curl http://UGREEN_IP:6333/healthz
curl http://UGREEN_IP:6333/collections
```

## SSH Tunnel (fallback if firewall blocks direct access)

```bash
# On Mac — forward local 6333 to ugreen's Qdrant
ssh -L 6333:localhost:6333 -L 6334:localhost:6334 -N ugreen

# Then in config.yaml set:
# qdrant:
#   host: localhost
#   port: 6333
```

## Management

```bash
# View logs
ssh ugreen "cd ~/qdrant && docker compose logs -f"

# Restart
ssh ugreen "cd ~/qdrant && docker compose restart"

# Stop
ssh ugreen "cd ~/qdrant && docker compose down"

# Backup data
ssh ugreen "docker run --rm -v qdrant_qdrant_data:/data -v ~/backups:/backup alpine tar czf /backup/qdrant-$(date +%Y%m%d).tar.gz /data"
```

## Config Notes

- Data persists in Docker volume `qdrant_data`
- Memory limit: 2GB (adjust in docker-compose.yml)
- Auto-restarts on server reboot (`restart: unless-stopped`)
- gRPC port 6334 used for faster vector operations
