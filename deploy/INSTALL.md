# Deploying ArcChime next to existing services (Ubuntu)

Templates, not run on your server by the author: read each step first. Assumes you already have a reverse
proxy (nginx or Caddy) terminating HTTPS for your subdomains. Nothing here touches your firewall or existing sites.

## 1. Pick a subdomain and a free local port
- Add a DNS A (or AAAA) record, e.g. `chime.your-domain.example` -> your server IP.
- Check the port is free: `ss -ltnp | grep 8810` (no output = free). Use another number if it is taken, in all places below.

## 2. Get the code
```bash
sudo apt install -y python3-venv git            # skip what you already have
sudo useradd --system --home /opt/arcchime --shell /usr/sbin/nologin arcchime
sudo git clone https://github.com/YOUR-USERNAME/arcchime.git /opt/arcchime
sudo python3 -m venv /opt/arcchime/.venv
sudo /opt/arcchime/.venv/bin/pip install -r /opt/arcchime/requirements.txt
sudo chown -R arcchime:arcchime /opt/arcchime
```
(No repo yet? `scp` the zip over and unzip into /opt/arcchime.)

## 3. Configure
```bash
sudo cp /opt/arcchime/deploy/arcchime.env.example /etc/arcchime.env
sudo nano /etc/arcchime.env     # ARCCHIME_PORT, ADMIN_TOKEN, DEMO_SECRET, DEMO_ADDRESS, DEMO_URL port
sudo chown root:arcchime /etc/arcchime.env && sudo chmod 640 /etc/arcchime.env
```
`DEMO_ADDRESS` must be an address you control that is NOT your sending wallet (it receives the shop payments).

## 4. Run it as a service
```bash
sudo cp /opt/arcchime/deploy/arcchime.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now arcchime
curl -s http://127.0.0.1:8810/health      # expect "chain":"eip155:5042" and a climbing block number
sudo journalctl -u arcchime -f
```

## 5. Add the site to your proxy
- Caddy: append `deploy/Caddyfile.example` to your Caddyfile, adjust domain/port, `sudo systemctl reload caddy`.
- nginx: use `deploy/nginx.conf.example`, `sudo nginx -t && sudo systemctl reload nginx`, then certbot.

## 6. Check from outside
`https://chime.your-domain.example/` (landing page with live status) and `/shop`.
Update later: `cd /opt/arcchime && sudo -u arcchime git pull && sudo systemctl restart arcchime`.

## Notes
- Keep `ADMIN_TOKEN`/`DEMO_SECRET` long and random; never commit `/etc/arcchime.env`.
- Back up `/var/lib/arcchime/arcchime.db` if you want to keep event history.
- A Dockerfile is also included if you prefer containers; then publish the container port to 127.0.0.1 only.
