# Deploying pr-reviewer on a server

Three units. The receiver answers GitHub in milliseconds, the worker spends
minutes per review, and the sweep catches whatever the webhook missed. They are
separate processes so none of those timescales can interfere with the others.

## Layout

```
/opt/pr-reviewer/          checkout + .venv
/etc/pr-reviewer/          pr-reviewer.env (0600), settings.json
/var/lib/pr-reviewer/      queue, scan lock
/srv/checkouts/<repo>/     a clone per reviewed repository
```

## Install

```bash
sudo useradd --system --home /var/lib/pr-reviewer --create-home pr-reviewer
sudo mkdir -p /opt/pr-reviewer /etc/pr-reviewer /srv/checkouts
sudo chown -R pr-reviewer:pr-reviewer /var/lib/pr-reviewer /srv/checkouts

sudo -u pr-reviewer git clone https://github.com/LeyouHong/pr-reviewer /opt/pr-reviewer
sudo -u pr-reviewer python3 -m venv /opt/pr-reviewer/.venv
sudo -u pr-reviewer /opt/pr-reviewer/.venv/bin/pip install -e /opt/pr-reviewer

sudo install -m 600 -o pr-reviewer deploy/pr-reviewer.env.example \
    /etc/pr-reviewer/pr-reviewer.env
sudo -e /etc/pr-reviewer/pr-reviewer.env          # fill in the secrets

# One clone per repository, named in settings.json as `checkout`.
sudo -u pr-reviewer git clone https://github.com/acme/api /srv/checkouts/api

sudo cp deploy/pr-reviewer-*.service deploy/pr-reviewer-scan.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pr-reviewer-serve pr-reviewer-worker pr-reviewer-scan.timer
```

Then nginx:

```bash
sudo cp deploy/nginx-pr-reviewer.conf /etc/nginx/sites-available/pr-reviewer
sudo ln -s /etc/nginx/sites-available/pr-reviewer /etc/nginx/sites-enabled/
sudo certbot --nginx -d reviewer.example.com
sudo nginx -t && sudo systemctl reload nginx
```

Finally, in each repository: Settings → Webhooks → Add webhook.
Payload URL `https://reviewer.example.com/webhook`, content type
`application/json`, secret = `GITHUB_WEBHOOK_SECRET`, events: **Pull requests**
only.

## Checking it works

```bash
curl -s http://127.0.0.1:8787/health            # queue depth
pr-reviewer queue --queue /var/lib/pr-reviewer/queue
journalctl -u pr-reviewer-worker -f
systemctl list-timers pr-reviewer-scan.timer
```

GitHub's webhook page has a **Recent Deliveries** tab showing every payload and
response — the first place to look when nothing happens. A `401` there means
the secret does not match; a timeout means the receiver is down and the sweep
will pick the work up within twenty minutes.

## Choices worth knowing about

**The worker restarts slowly and gives up loudly.** `RestartSec=60` with
`StartLimitBurst=5` over ten minutes: the worker exits deliberately when the
account cannot pay, and a five-second restart loop would hammer the API with
requests that are all going to be refused. Five rapid failures stop the unit
and leave it stopped, because an expired token or an empty balance needs a
person and retrying forever hides it. Queued jobs survive either way.

**`TimeoutStopSec=900`.** A review is minutes of model calls; killing one
mid-flight throws that spend away. A job interrupted anyway returns to the
queue when its claim expires.

**`ReadWritePaths` includes `/srv/checkouts`, not just the clones.** Worktrees
are created *beside* each clone, so the parent has to be writable. Getting this
wrong produces a setup that works until the first review with validation
enabled.

**The sweep timer is `Persistent=yes` with a randomised delay.** A host that
was off runs the sweep once on wake rather than skipping the window — it exists
precisely for missed events — and the jitter stops a fleet calling the GitHub
API in unison.

**`/health` is restricted to your own networks.** It reports queue depth, which
is operational detail, not public information.
