# Code-server Installation Guide for hermes

This directory contains scripts to install and run Code-server as a local web IDE on your vps-hosted Hermes agent.

## Prerequisites

1. SSH access to vps
2. Code-server installed

## Installation Steps

```bash
cd /home/hermes/workspace/repositories/YauheniPo/popot-bot-2.0/hermes/vscode-server
./deploy.sh
```

The script will:
- Create systemd service for Code-server
- Set up SSH tunneling (default port: localhost:8080 -> 3000)
- Configure max connections
- Set file sharing paths to repositories

## SSH Tunneling Setup
```bash
ssh -L 8080:localhost:3000 hermes@VPS_IP
```

## Configuration

- [Dockerfile](Dockerfile) - Container setup
- [systemd](Code-server.service) - Service management
- [config](config.json) - Advanced settings