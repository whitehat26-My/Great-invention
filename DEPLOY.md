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

## Picking a host

| | cost | needs a card | good for |
|---|---|---|---|
| A machine you own — mini PC, old laptop, Pi 5 | none, if you have one | no | one restaurant, no monthly bill, data stays on site |
| A Malaysian VPS (Exabytes, ServerFreak, …) | ~RM20–40/mo | often FPX or local transfer | no hardware to mind, someone else's power and internet |
| Hetzner, Contabo, DigitalOcean | €4–6/mo | yes | cheapest per unit of RAM, pay in EUR/USD |

Because nothing needs a public address, the first row is a real answer rather
than a compromise — the usual reason to rent a server is to be reachable from
the internet, and this is not reachable from the internet by design.

What a host does need: **stay on**. A laptop that sleeps when the lid closes
stops the restaurant's brain. On Linux:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target
```

Give it about 2 GB of RAM and 10 GB of disk to be comfortable.
