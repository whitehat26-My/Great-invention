# Running it for real

Everything so far has run while a terminal was open. This is how it keeps
running when it is not.

## The one fact that decides everything else

**Nothing here needs a public address.** The Telegram listener uses long
polling: it dials out to `api.telegram.org` and holds the connection open. The
approvals, the questions, the instructions — all of it arrives over a connection
the restaurant opened.

So there is no inbound port to forward, no domain to buy, no TLS certificate to
renew, and no firewall hole to get wrong. Anything with power and internet can
host this: a mini PC under the counter, an old laptop, a Raspberry Pi 5, or a
€5 VPS. That is the whole hosting requirement.

(The one exception is the web dashboard, which does need a reachable address —
but only from wherever you look at it. On a machine in the restaurant that is
your own wifi, and `http://<that machine>:8000/dashboard?key=…` is enough.)

## What has to be running

Five processes and two services:

| | what it does | what breaks without it |
|---|---|---|
| `postgres` | everything the agents know | all of it |
| `redis` | the queue between beat and worker | scheduled work never runs |
| `beat` | decides *when* work happens | no morning prep, no nightly close, no brief |
| `worker` | does the work beat schedules | jobs queue up and nothing happens |
| `listener` | approvals, questions, instructions | **the bot goes deaf** |
| `api` | webhooks, dashboard, system map | POS ingestion and the web views |

The listener is the one people forget, because the restaurant runs perfectly
without it and simply never answers the phone.

**Exactly one listener, ever.** Telegram allows a single `getUpdates` at a time;
two listeners fight, and each drops about half the messages. Never scale that
service.

## Deploying

```bash
git clone https://github.com/whitehat26-My/Great-invention.git
cd Great-invention
cp .env.example .env    # then fill it in — see below
docker compose up -d --build
docker compose ps       # all services 'running', migrate 'exited (0)'
```

`migrate` runs once to completion before anything reads the database, so a fresh
host does not start five processes against an empty schema and report the
resulting crashes as five unrelated faults.

A missing `.env` is not fatal — every setting has a working default, and compose
treats the file as optional — but without one the platform runs in simulation
(no Telegram, no model), so the `.env` step is where it becomes *your*
restaurant.

Everything long-running carries `restart: unless-stopped`. This is unattended
kitchen equipment: nobody is watching at 03:00, and a process that dies and
stays dead looks exactly like one working quietly.

### The `.env` it needs

```
LLM_PROVIDER=google
GOOGLE_API_KEY=...
GOOGLE_MODEL_CONVERSATIONAL=gemini-3.5-flash
GOOGLE_MODEL_REASONING=gemini-3.6-flash

TELEGRAM_BOT_TOKEN=...          # BotFather
TELEGRAM_CHAT_ID=...            # only this chat may ask, instruct or approve
APPROVAL_CHANNEL=telegram
APPROVAL_API_KEY=<long random>  # guards the dashboard and approval endpoints

POSTGRES_PASSWORD=<not 'restaurant'>
TZ=Asia/Kuala_Lumpur
```

Two of these are load-bearing in ways that are quiet when wrong:
`TELEGRAM_CHAT_ID` is the allow-list, so a wrong one means every message is
ignored *in silence*; `APPROVAL_API_KEY` unset means the dashboard refuses to
serve rather than serving anyone.

### Proving it worked

```bash
docker compose exec api restaurant-ai doctor
```

Checks every link and changes nothing. The line that matters is the last one:

```
  ok    listener        running — something is reading the chat
```

If it says `NOT RUNNING`, the bot is deaf whatever else is green. Then message
the bot: `/help` should come straight back.

### Seeding a first menu

```bash
docker compose exec api restaurant-ai import-menu /path/to/menu.xlsx \
    --allow-uncosted --replace-menu
```

## Keeping it alive

**Back up the database.** It is the only thing here that cannot be rebuilt from
the repository — every agent decision, approval and closed day lives in it.

```bash
docker compose exec -T postgres pg_dump -U restaurant restaurant_ai \
    | gzip > backup-$(date +%F).sql.gz
```

Run it nightly from cron, keep a fortnight, and copy one off the machine. A
backup that only exists on the machine it protects is not a backup.

**Watch the logs when something is odd:**

```bash
docker compose logs -f listener   # what the bot heard and said
docker compose logs -f beat       # what fired, and when
```

**Upgrading:**

```bash
git pull && docker compose up -d --build
```

`migrate` runs again on the way up, so schema changes apply themselves.

## On the machine in front of you: `restaurant-ai up`

Docker Compose is the right answer on a server. On the machine people actually
have — a Windows laptop, an old desktop behind the till — it is four terminals
kept open by hand, and the restaurant is only as alive as the most forgettable
of them. The forgettable one is always the listener, and a closed listener
window is a deaf bot with no error anywhere.

```powershell
restaurant-ai up
```

One window runs the lot: listener, beat, worker, API. One Ctrl-C stops the lot,
process tree and all — a Celery worker's forked pool goes down with the worker,
never orphaned. A child that dies at 03:00 is restarted with backoff, because
nobody is there to do it by hand; a child that dies instantly every time is
reported and given up on, because restarting a bad config forever is not
resilience. It refuses to start at all if Postgres is not reachable, with the
command that fixes it, rather than filling the screen with five processes'
tracebacks that all mean the same thing.

It sets the database up itself — starting it, and applying migrations before
anything reads it, which is the same `migrate` step compose runs. If Postgres is not reachable and Docker Desktop
is installed and running, `up` runs `docker compose up -d postgres redis` for
you and waits for Postgres to answer before starting anything that needs it.
When it cannot, it says which of the three situations this machine is in — no
Docker (install Docker Desktop, once), Docker installed but not running (open
it from the Start menu), or the containers failed (Docker's own output says
why) — because "start it first" is advice that gets answered with "how?".

### Starting it with Windows

So a reboot does not mean a deaf bot (run once, from the project folder, in the
virtualenv — no administrator needed):

```powershell
restaurant-ai install-startup
```

It writes a launcher into your own Startup folder, so Windows opens the
restaurant's window at every logon; `--remove` undoes it. (`schtasks` is the
textbook answer and it replies "Access is denied" from a normal prompt — the
Startup folder is yours already.)

Then stop the laptop sleeping with the lid open: Settings → System → Power →
"When plugged in, put my device to sleep" → **Never**. A sleeping laptop is the
restaurant's brain off, however healthy every process was when the lid dimmed.

This is the honest budget option, and its honest limits: the bot is only awake
while that machine is on and on wifi. For always-on, the sections below.

## Reaching the dashboard from anywhere

Telegram works from anywhere because the listener dials out. The dashboard and
the system map are the opposite: a browser has to reach *in*, which on a laptop
behind a home router means port forwarding, a domain and a certificate — three
things to get wrong, one of which is a hole in the restaurant's network.

A Cloudflare quick tunnel dials out too, so there is still no inbound port, no
domain and no certificate.

```powershell
winget install --id Cloudflare.cloudflared     # once
restaurant-ai tunnel                            # leave it running
```

It prints the address and sends both links to the approvals chat, because a
quick tunnel's name is random and changes each restart — an address the owner
has to go and find is an address they will not use.

**The link is the credential.** A browser address bar cannot set a header, so
the key travels in the URL exactly as it does on the local dashboard. Anyone the
link is forwarded to can read the restaurant's numbers. `restaurant-ai tunnel`
refuses to start at all when `APPROVAL_API_KEY` is unset, since a public address
for a system that refuses to serve is a locked door with a sign on it.

Before this existed, `GET /agents` and `GET /agents/runs` answered without
credentials — harmless while they only ever answered on localhost, and not
harmless with a public address, because a run summary is the restaurant's
business. The whole `/agents` router is now behind the key, and a test walks the
app's own schema asserting that every endpoint returning data refuses an
anonymous caller, so a new route joins that check by existing.

## The most powerful free host

**Oracle Cloud Always Free** gives you, free and not as a trial: 4 ARM cores and
**24 GB of RAM**, 200 GB of storage, 10 TB/month of traffic. That is several
times the machine a €5 VPS rents you, and this platform needs about 2 GB — so
it is not a compromise, it is over-provisioning by a factor of ten.

Three honest caveats, because none of them are in the marketing:

1. **It asks for a card to verify who you are.** It charges about a dollar and
   refunds it, and the Always Free resources genuinely never bill. But if a card
   is the thing that blocked Railway, this blocks in the same place — skip to
   "a machine you own" below, which is a real answer and not a consolation.
2. **The ARM capacity is often exhausted.** "Out of host capacity" on the free
   ARM shape is common in popular regions and people retry for days. Singapore
   is nearest to Malaysia and busy; try creating the instance at an odd hour,
   and try the other Asian regions before giving up.
3. **It is ARM, not Intel.** Everything here has arm64 wheels
   (`psycopg[binary]`, `pydantic-core`, `uvloop`) and the image carries
   `build-essential` for anything that does not, so `docker compose up --build`
   is the same command. It is untested on ARM by me — this sandbox is x86_64 —
   so expect the first build to take longer and tell me if anything fails.

Once the instance exists (Ubuntu 22.04+, "Ampere" shape, 4 OCPU / 24 GB):

> **These commands run on the server, in its Ubuntu terminal** — connect with
> `ssh ubuntu@<the instance's IP>` first. They are Linux commands; pasted into
> PowerShell on your own laptop they fail on the first `&&` (and `sudo` is not
> a Windows thing). On your laptop, use `restaurant-ai up` instead.

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
newgrp docker

git clone https://github.com/whitehat26-My/Great-invention.git
cd Great-invention
cp .env.example .env
nano .env                              # the values listed above
docker compose up -d --build
docker compose exec api restaurant-ai doctor
```

**Oracle's networking is famously fiddly — and none of it applies here.** Free
instances have both a cloud security list and host `iptables`, and getting a
port through both is where most people lose an evening. This needs no inbound
port at all, so you can leave every one of those defaults alone.

The one thing to set is the timezone, or the nightly close fires at the wrong
hour: `sudo timedatectl set-timezone Asia/Kuala_Lumpur`.

## Picking a host

| | cost | needs a card | good for |
|---|---|---|---|
| **Oracle Cloud Always Free** | free forever | yes, to verify | far the most powerful free option: 4 ARM cores, 24 GB |
| A machine you own — mini PC, old laptop, Pi 5 | none, if you have one | **no** | no bill, no card, data stays on site |
| A Malaysian VPS (Exabytes, ServerFreak, …) | ~RM20–40/mo | often FPX or local transfer | no hardware to mind, someone else's power |
| Hetzner, Contabo, DigitalOcean | €4–6/mo | yes | cheapest per unit of RAM, pay in EUR/USD |
| Google Cloud `e2-micro` | free forever | yes | 1 GB RAM — too tight for Postgres plus four workers |

Two free tiers that look right and are not: **Render**'s free Postgres expires
after a month and its free services sleep when idle, which for a listener means
deaf; **Railway**'s free allowance is trial credit, not a standing tier.

Because nothing needs a public address, the first row is a real answer rather
than a compromise — the usual reason to rent a server is to be reachable from
the internet, and this is not reachable from the internet by design.

What a host does need: **stay on**. A laptop that sleeps when the lid closes
stops the restaurant's brain. On Linux:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target
```

Give it about 2 GB of RAM and 10 GB of disk to be comfortable.
