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

Then stop the machine sleeping: Settings → System → Power → "When plugged in,
put my device to sleep" → **Never**. A sleeping machine is the restaurant's
brain off, however healthy every process was a moment before.

On a desktop, set the BIOS to power on after a cut — usually "Restore on AC
Power Loss" or "AC Back" under power management. Without it, the first power cut
while nobody is in stops the restaurant until someone comes and presses the
button, and `install-startup` cannot help because it needs a logon that never
happens.

This is the honest budget option, and its honest limits: the bot is only awake
while that machine is on and on wifi. For always-on, the sections below.

## Running it on Claude, and what it costs

An Anthropic key is bought as **credits, not a subscription** — top up $5, spend
$5, nothing recurs. (A Claude Pro/Max subscription is the chat app and does *not*
include an API key; they are separate products with separate bills.) Prepaid
credit is also the safety net: when it runs out the calls fail, they do not
silently become an invoice.

```
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
MODEL_REASONING=claude-sonnet-5
MODEL_CONVERSATIONAL=claude-haiku-4-5
```

The defaults are Opus 5 and Sonnet 5, which are the right models for hard
reasoning and the wrong ones for a first top-up. Sonnet for the agents that
decide things and Haiku for chat costs roughly a fifth as much, and neither is
straining on "did the flour run low".

**Turn prompt caching on in the console.** Every run of an agent sends the same
system prompt and the same tool schemas; cached, that part bills at a tenth.
It is the single biggest lever here and it is off by default.

### Free instead: Hermes on your own machine

An open model running locally costs nothing per call, has no daily cap, and
needs no key. `LLM_PROVIDER=ollama` points the platform at it.

```
LLM_PROVIDER=ollama
OLLAMA_MODEL_REASONING=hermes3:latest
OLLAMA_MODEL_CONVERSATIONAL=hermes3:latest
```

After any `git pull`, run `pip install -e .` and then `restaurant-ai migrate` before
starting. Pulling brings
code; it does not bring the packages that code needs, and a provider added
upstream arrives as a missing module rather than as anything resembling a
version problem.

Installing it, on Windows, with no admin rights:

```powershell
$ProgressPreference = 'SilentlyContinue'   # see below — not optional at this size
Invoke-WebRequest -Uri "https://ollama.com/download/OllamaSetup.exe" -OutFile "OllamaSetup.exe"
.\OllamaSetup.exe
```

The first line matters here in a way it does not for `cloudflared`. Windows
PowerShell buffers the whole download in memory and redraws its progress bar
constantly, which on a 1.5GB installer costs more time than the transfer does.
`curl.exe -L -o OllamaSetup.exe <url>` is the alternative — it ships with
Windows and streams straight to disk.

Then **open a new terminal** before `ollama pull hermes3` — the installer
puts `ollama` on the PATH, and a window opened before that never sees it. The
model is about 5GB on disk and roughly the same in RAM while it answers.

Then check what it actually saved as — `ollama list` names it, and a plain
`pull hermes3` leaves `hermes3:latest` rather than a size-tagged one. Whatever
that column says is what belongs in .env; `restaurant-ai models` reads the same
list back from the platform's side.

Hermes is the right open model to reach for here because the agents **bind
tools**, and Hermes is trained for tool calling. A general chat model that
cannot emit a tool call does not fail — it runs the loop to its iteration limit
doing nothing, and reports that it ran out of turns.

**A graphics card changes the arithmetic and the speed together.** Ollama uses
one if it finds one, and a model with 12GB of VRAM to live in answers in
seconds rather than minutes *and* costs the machine's own memory almost nothing,
because it is not resident there at all. Without one, budget roughly 4GB for
Windows, 1GB for Postgres and Redis, 1.5GB for the four Python processes and
5GB for the model — about 11.5GB of 16, with room for a browser. 8GB and no
graphics card is where the 3B model becomes the right choice instead.

`restaurant-ai doctor` says which it is, because the same command on the same
machine means different things depending on the answer:

    ok  language model  ollama — hermes3:latest, answering, on the graphics card

Two honest limits:

- **It is slower by a lot.** Seconds become minutes on a CPU. Fine at 06:00
  when nobody is waiting; not fine when the owner has asked a question.
- **It reasons less well.** An 8B model will pick the wrong tool or the wrong
  arguments more often than Sonnet does. What saves you is that every action
  worth worrying about is already behind an approval — a bad decision arrives
  as a proposal you reject, not as a purchase order that happened.

### The split: local for the work, cloud for the conversation

Those two limits point the same way, so the setting exists to act on it:

```
LLM_PROVIDER=ollama                  # the ~78 scheduled calls/day
LLM_PROVIDER_INTERACTIVE=anthropic   # the ~20 the owner waits on
ANTHROPIC_API_KEY=sk-ant-...
MODEL_CONVERSATIONAL=claude-haiku-4-5
```

The scheduled agents run free on the machine under the counter, taking whatever
time they need. The owner's questions go to a hosted model and come back in
seconds. That is roughly **$2/month instead of $9–16**, and the bot never waits
on a quota.

`LLM_PROVIDER_INTERACTIVE` unset means one provider for everything, which is
what every existing `.env` already means.

`restaurant-ai doctor` reports both halves when they differ — one line naming
only one of them would leave the other unchecked, and the unchecked half is the
one nobody notices is broken.

### What actually spends the money

Not the owner's questions — ten of those a day is noise. It is the schedule, and
it is lopsided:

| | runs/day | share |
|---|---|---|
| `order_pacing` (every 5 min, 11:00–23:59) | 156 | **83%** |
| `reputation` (hourly) | 24 | 13% |
| everything else, all nine agents | ~7 | 4% |

The pacing agent is scheduled that often because a ticket landing at 19:03 has
to reach the pass by 19:08 — not because there is a ticket every five minutes.
It now checks whether the kitchen is empty *before* reasoning, and ends the run
there when it is. On a restaurant whose till is not connected yet, that is all
156 of them, for nothing.

That check is also why the Gemini free tier kept failing to answer: 20 requests
per day, spent before opening, so the owner's question at lunchtime met a
45-second rate-limit wait and looked like a dead bot.

### Rough monthly cost

Estimated from the schedule above at ~5k input and ~400 output tokens per call —
your mileage varies with menu size and how much the agents find to do. Watch
"Spend this month" in the console for the real figure.

| | without caching | with caching |
|---|---|---|
| Haiku for both tiers | ~$16 | ~$9 |
| Sonnet reasoning + Haiku chat | ~$25 | ~$14 |

Before the pacing check, the same schedule was ~$100/month — a $5 top-up lasted
about a day. Both columns above assume it is in.

## Reaching the dashboard from anywhere

Telegram works from anywhere because the listener dials out. The dashboard and
the system map are the opposite: a browser has to reach *in*, which on a laptop
behind a home router means port forwarding, a domain and a certificate — three
things to get wrong, one of which is a hole in the restaurant's network.

A Cloudflare quick tunnel dials out too, so there is still no inbound port, no
domain and no certificate.

```powershell
# once — winget is absent from older Windows installs, so the reliable form is
# the single .exe, downloaded into the project folder, no admin needed
Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile "cloudflared.exe"

restaurant-ai up --with-tunnel
```

`--with-tunnel` puts the tunnel in the same window as everything else, under the
same supervision, and that is the point rather than a convenience. A tunnel in
its own window is a window someone has to remember, and the forgettable window
is the one that gets closed — the lesson the listener already taught. Nothing
reports its absence; the dashboard is simply unreachable from outside, with no
error anywhere.

Supervised, the quick tunnel's changing name stops being worth solving. It gets
a new address on every restart and sends the new link to the phone that opens
it, so the address being random costs nothing.

`restaurant-ai tunnel` still runs one on its own, for a machine where the rest
is already running under compose.

`restaurant-ai tunnel` looks for `cloudflared` on the PATH *and* in the project
folder, and runs it by full path — relying on Windows searching the working
directory is relying on a default that has been tightened before.

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

### What Oracle costs you that the numbers do not show

Its 24 GB is more memory than a desktop with 16 GB. It is also **the wrong
comparison**, because memory was never the constraint — a graphics card was.

A machine with a 12 GB GPU answers a Hermes prompt in about five seconds. The
free Oracle shape has four ARM cores and no GPU at all, so the same prompt takes
minutes. Nothing is broken; the arithmetic is simply being done by a processor
instead of a card built for it, and that is a difference of thirty to fifty
times.

Which matters entirely depending on who is waiting:

| | on the desktop's GPU | on Oracle's CPU | does it matter |
|---|---|---|---|
| Scheduled agent runs | seconds | minutes | **no** — nobody is waiting at 06:00 |
| The owner's questions | seconds | minutes | **yes** — a bot that takes three minutes is a dead bot |

So a machine with no GPU is not a bad host. It is a bad host *for the chat*,
and that is exactly what `LLM_PROVIDER_INTERACTIVE` exists to solve: Hermes runs
free on the server for the eighty-odd scheduled calls a day nobody waits on, and
the twenty the owner waits for go to a hosted model and come back in seconds.

```
LLM_PROVIDER=ollama                  # free, slow, and nobody is waiting
LLM_PROVIDER_INTERACTIVE=anthropic   # seconds, for the owner
ANTHROPIC_API_KEY=sk-ant-...
MODEL_CONVERSATIONAL=claude-haiku-4-5
```

That is free hosting, free agent inference, and roughly $2 a month for a chat
that answers immediately — against about RM20 a month of electricity to leave a
desktop running, and a restaurant that goes dark when the power does.

The honest case for keeping the desktop: it is faster at everything, the data
never leaves the building, and there is no account to lose. The honest case
against: it is off when the power is off, and nobody is there at 3am to turn it
back on.

### Creating the instance

Only you can do this part — it needs your account and your card.

1. **oracle.com/cloud/free** → Start for free. Pick the region nearest you;
   **Singapore** is closest to Malaysia and, for the same reason, the busiest.
   The region cannot be changed later, so if Singapore refuses capacity for days,
   starting again in another Asian region is a new account.
2. It asks for a card to prove you are a person. It charges about a dollar and
   refunds it; Always Free resources never bill.
3. **Compute → Instances → Create instance.**
   - Image: **Ubuntu 22.04** or newer
   - Shape: **Change shape → Ampere → VM.Standard.A1.Flex**, then set
     **4 OCPUs and 24 GB**. This is the whole free allowance in one machine.
   - **Download the private key** when it offers. There is no second chance, and
     without it you cannot get in.
4. Note the **public IP address**.

**"Out of host capacity" is the normal outcome, not a mistake.** The free ARM
shape is heavily contested. People retry for days. Try at odd hours, and try
`VM.Standard.A1.Flex` with fewer cores if the full four will not create — 2
OCPUs and 12 GB still runs all of this.

### Then, on the server

```bash
ssh -i /path/to/your-key ubuntu@<the ip>

git clone https://github.com/whitehat26-My/Great-invention.git
cd Great-invention
bash scripts/setup_server.sh
```

It runs three times on purpose, and says why each time:

1. **First run** installs Docker and stops — group membership only applies to a
   new login, so it asks you to log out and back in.
2. **Second run** writes a `.env` with every setting named and blank, and stops.
   Fill it in with `nano .env`. It never overwrites a `.env` that exists: this
   file holds the only secrets on the machine, and a setup script that clobbers
   them is one nobody dares run twice.
3. **Third run** builds, starts everything, and prints `doctor`.

It refuses rather than half-starting. A missing bot token gives a deaf bot and a
missing dashboard key gives pages that refuse to serve — both silent, both
discovered hours later — so it names what is still blank and waits.

**No firewall rules. No open ports.** The listener dials out and the dashboard is
reached through a tunnel that also dials out, so every Oracle networking default
can be left exactly as it is. That is the part people lose an evening to.

**Oracle's networking is famously fiddly — and none of it applies here.** Free
instances have both a cloud security list and host `iptables`, and getting a
port through both is where most people lose an evening. This needs no inbound
port at all, so you can leave every one of those defaults alone.

The one thing to set is the timezone, or the nightly close fires at the wrong
hour: `sudo timedatectl set-timezone Asia/Kuala_Lumpur`.

## Running the till on the same provider

A Malaysian VPS is the sensible choice for a Malaysian restaurant: FPX rather
than a card, support in the same timezone, and the data stays in the country,
which matters the day anyone asks about PDPA. Buy a **VPS with root access**,
not shared hosting — a cPanel plan cannot run Docker, and that is the mistake
that is easy to make and expensive to unwind.

The temptation once you have a server is to put the till on it too. One bill,
one machine, one thing to remember. It is worth separating two questions that
sound like one.

**The same provider is a good idea.** One relationship, one payment method, one
support line that answers in your timezone.

**The same server is not.** These two systems are not equally important and
should not share a fate. If this platform's database eats the memory on a
Tuesday afternoon, the till stops taking orders — and a restaurant that cannot
take an order is shut. Nothing here is worth that. Two small VPSes at the same
provider cost a few ringgit more than one larger one and cannot take each other
down.

**And the till probably should not be in the cloud at all.** A cloud POS stops
selling when the internet drops, which on restaurant wifi is not a hypothetical.
The till belongs on a machine in the building, where a dead line is a slow
afternoon rather than a closed one.

That gives the shape worth building toward:

    the till, in the restaurant  ──POST /webhooks/pos──▶  this platform, hosted

The till keeps working through an outage and sends what it recorded when the
line comes back. This platform is always on, always reachable from a phone, and
never something the restaurant depends on to take money.

It is also why the POS stays its own repository. The two systems have different
uptime requirements, different failure consequences and different release
rhythms, and a shared deployment is the fastest way to give the important one
the release rhythm of the unimportant one.

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
