# systemd units for Ms Tanya (replaces the `&` background jobs of run_linux.sh in production)

Install (as root), code in /opt/ms-tanya with its .venv and .env, service user `tanya`:

    cp deploy/systemd/*.service deploy/systemd/tanya.target /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now tanya.target
    systemctl status 'tanya-*'          # every process, one line each
    journalctl -u tanya-turn@3 -f       # logs of one lane

Every unit restarts by itself 2 s after any exit (Restart=always). Workers already survive Redis/network errors
inside the process (workers.forever, StreamWorker.run_forever); systemd covers a crash or kill of the process.
Qdrant: run with compose.qdrant.yml (self-healing start, restart unless-stopped). Redis: AOF everysec.
