# Deploy ke VPS

Dibuat lengkap di **Fase 5** (user Linux `indodax`, virtualenv, `.env` chmod 600, chrony/NTP,
systemd `Restart=always`, journalctl, update, stop darurat).

Untuk sekarang (Fase 1) yang bisa dicoba di VPS — tanpa sudo, tanpa API key, tanpa order:

```bash
git clone <repo> indodax-agent && cd indodax-agent
python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m scripts.smoke_public
```
