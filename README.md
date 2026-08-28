# Autonomous AI Restaurant

An operations platform for a restaurant run by agents. Thirteen of them, across
six departments, handling the administrative, analytical and operational work
while people cook and serve.

It runs. `make up && make migrate && make seed && make simulate` replays a full
service day — orders arriving through signed webhooks, stock draining through
recipe bills of materials, purchase orders drafted and held for a human, the
books closed at 23:30 — and prints the end-of-day report.

No API key is needed. `LLM_PROVIDER=fake` runs every agent on its deterministic
path, which is also how the test suite runs.

---

## The design decision everything else follows from

**The LLM does judgement; plain Python does the arithmetic.**

Forecasting, reorder points, plate costing, margin analysis, roster fitting and
reconciliation live in `domain/` — pure functions, no I/O, no model calls,
covered by 200-odd unit tests. Agents reach them as tools.

So when the stock agent decides to order 10 cases of coconut milk, that number
came from `reorder_point(avg_daily_usage, lead_time, sigma, service_level)`, not
from a model's impression of how much coconut milk feels right. The model's job
is to read the situation, choose the action, and explain it to a human.

The second decision, which follows from the first: **an agent proposes; a human
disposes.** Five agents can spend money or publish something public, and all
five stop.

---

## The eleven agents

### Front of House
| Name | Agent | What it does |
|---|---|---|
| **Aziera** | Feedback & Reputation | Sweeps Google/social hourly, classifies, drafts replies, escalates the serious ones — **approval-gated** |

> Reservations (Freddy) and conversational ordering (Melissa) were retired. Both
> conversed with guests live under their own names, and the order agent gave
> allergen advice — a wrong answer there is a different kind of wrong from a
> wrong purchase order. This is a management platform: nothing in it writes to a
> guest unprompted. Aziera stays because she is already that shape, drafting
> replies a human sends. Both live in git history if a booking or ordering
> channel is ever wanted back.

### Kitchen & KDS
| Name | Agent | What it does |
|---|---|---|
| **Betrisha** | Dynamic Prep Forecaster | Forecasts per-item demand, explodes it to ingredient quantities, grosses up for yield loss, scores yesterday and corrects |
| **Ciknor** | Order Routing & Pacing | Routes lines to stations and back-times each so a table's plates land together |

### Supply Chain
| Name | Agent | What it does |
|---|---|---|
| **Rain** | Stock Tracking & Auto-Reorder | Recalculates reorder points from real usage and drafts POs when stock trips them — **approval-gated** |
| **Suri** | Supplier & Invoice | Three-way match of PO, goods receipt and invoice; catches price creep — **approval-gated** |

### Marketing & Revenue
| Name | Agent | What it does |
|---|---|---|
| **Franky** | Social Media & Content | Drafts posts and win-back offers for dormant diners — **approval-gated** |
| **Irma** | Dynamic Pricing & Menu Engineering | Star/Plowhorse/Puzzle/Dog classification and guardrailed price proposals — **approval-gated** |

### Workforce
| Name | Agent | What it does |
|---|---|---|
| **Henry** | Shift Scheduling | Shapes shifts against the hourly demand curve and fits a roster inside availability, hours caps and rest rules |
| **Kaksu** | Staff Assistant & Onboarding | Answers SOP and recipe questions; validates and routes shift swaps |

### Finance
| Name | Agent | What it does |
|---|---|---|
| **Emil** | Bookkeeping & Reconciliation | Squares POS takings against card settlements, platform payouts and the bank; posts double-entry journals |
| **Camelia** | Daily Performance | Prime cost, labour ratio, food cost, operating margin, and what moved them |

---

## How an agent works

All eleven are the same compiled LangGraph graph. Only the `AgentSpec` differs
— prompt, tools, model tier, approval policy.

```
              ┌────────── more to do ──────────┐
              ↓                                │
perceive ─→ reason ─→ act ─┬─ nothing gated ───┴────────→ record
                           └─ await_approval ─→ commit ──→ record
```

- **perceive** — loads this agent's read-only view of the world. No LLM, no writes.
- **reason** — the LLM bound to this agent's tools, or its deterministic path when `LLM_PROVIDER=fake`.
- **act** — runs the tools, and answers each one. A gated tool returns a *proposal* instead of acting.
- **await_approval** — calls `interrupt()`. The graph checkpoints to Postgres and the process **unwinds**.
- **commit** — performs the approved proposals in one transaction.
- **record** — writes the audit trail and publishes domain events.

Splitting `act` from `commit` is the point. Preparing an action and performing
it are separate steps with a human in between, and the agent has no path from
one to the other.

The split has to be real, though, and twice it was not. A gated tool that does
its own publishing inside `act` has already acted by the time anyone sees the
proposal — Franky scheduled posts straight to the platforms, harmless only
because `SOCIAL_PROVIDER` defaults to `fake`. And a `gate_when` narrower than
what its tool can change lets the rest through: Irma gated on price changes,
so three bundles went out unapproved. A drafted post now carries no
`external_ref` and a drafted offer no `issued_count`, which is what makes
prepared distinguishable from done without a status column to forget to set.

The loop back from `act` to `reason` exists only on the live-model path, and it
is the difference between an agent and a one-shot planner. A model that never
sees its tool results cannot look up a table and then book it — the id it needs
is in a result it was never shown — and its closing summary describes what it
*meant* to do rather than what happened. The deterministic path does not loop:
it decides its whole plan up front and already knows the outcome.

The loop stops at the gate. When a tool proposes something gated the run parks,
and no number of remaining iterations lets the model carry on past a human.

Because the checkpointer is Postgres-backed, an approval **survives a deploy**.
Verified by running an agent in one interpreter and approving from a second that
knew only the thread id — the purchase order committed only after the second
process said yes.

---

## What "approval-gated" actually means

```
07:00  Morning reorder sweep (stock_reorder)
??     3 purchase order(s) totalling 5089.80, 18 urgent line(s)
```

and in Slack:

```
Hock Seng Dry Goods (PO-260827-001) — 4093.80, deliver 2026-09-01
  - Coconut milk: 37325ml on hand (2.7 days cover) is at or below the reorder
    point of 50988. Ordering 10 x 12000ml pack to reach 146230 (7d cover + 3d
    lead time).
  - Kway teow, flat rice: 8448g on hand (1.7 days cover) is at or below the
    reorder point of 18869. Ordering 6 x 5000g pack to reach 33664 (3d cover +
    3d lead time, capped from 7d by 4d shelf life).
```

Every line says what is on hand, how many days that covers, and what it is
ordering to reach. The human can judge it without opening a database.

Gated by default: purchase orders, supplier payments, menu price changes and
bundles, replies to poor reviews, social posts, win-back offers, and anything
over `APPROVAL_VALUE_THRESHOLD`. Policy
lives on the tool, not in agent code, so "this needs a human" is a property of
the action and cannot be forgotten by the next agent that performs it.

A gated tool that proposed *nothing* does not interrupt. Waking someone to
approve an empty purchase order is how people learn to rubber-stamp the ones
that matter.

---

## The bill of materials

The BOM is the spine. `recipe_component` is self-referencing, so a plated dish
consumes sub-recipes which themselves consume raw ingredients.

Selling one Nasi Lemak Ayam Rendang resolves to **20 raw ingredients** through
two levels:

```
Nasi Lemak Ayam Rendang       RM24.90   cost RM9.72   food cost 39.0%
  Chicken thigh          180.0000 g    RM3.3300
  Coconut milk           176.0000 ml   RM1.6544     ← 80ml direct + 96ml via coconut rice
  Anchovies, dried        20.0000 g    RM1.2800
  Jasmine rice           160.0000 g    RM0.8320     ← only via the coconut rice sub-recipe
  ...
  Belacan (shrimp paste)   2.0000 g    RM0.0760     ← only via the sambal sub-recipe
```

One walk of that tree gives you three things at once:

- **Stock deduction.** A POS sale moves every one of those 20 lines.
- **Plate cost**, and therefore margin, and therefore menu engineering.
- **Allergens.** Derived, never hand-labelled. Nasi lemak is correctly
  `shellfish` because belacan is inside the sambal — which is exactly the case a
  maintained-by-hand allergen label gets wrong.

```
[REFUSE] Nasi Lemak Ayam Rendang vs shellfish
         shellfish is in Belacan (shrimp paste)
         -> Cannot be made safe. Offer an alternative.
[ADAPT ] Nasi Lemak Ayam Rendang vs peanut
         peanut is in Peanuts, roasted
         -> Prepare without Peanuts, roasted.
```

---

## Quick start

```bash
make install     # venv + dependencies
make up          # Postgres + Redis, no Docker daemon needed
make migrate     # schema
make seed        # demo restaurant + 8 weeks of trading history
make simulate    # a full service day, end to end
```

`make up` uses `scripts/dev_up.sh`, which initialises a throwaway Postgres
cluster and Redis under `.devdata/`. `docker-compose.yml` is there for a normal
workstation; the script exists because sandboxes and CI containers often have no
Docker daemon.

### Running it for real

```bash
make api         # FastAPI webhook receiver on :8000
make worker      # Celery worker
make beat        # Celery beat — the operating rhythm below
make test        # 558 tests
make check       # everything CI runs: lint, format, typecheck, tests
```

Useful commands:

```bash
restaurant-ai agents                     # the 11, by department
restaurant-ai run-agent stock_reorder    # run one now
restaurant-ai approvals                  # what is waiting for a human
restaurant-ai approvals --resolve <id>   # approve it
restaurant-ai menu-cost MNU-NASILEMK     # plate cost, exploded
restaurant-ai simulate-day --auto-approve
```

---

## Running it on real models

Everything above runs with no key. Two live providers are supported.

**Gemini — free, no card.** Get a key at [aistudio.google.com](https://aistudio.google.com):

```bash
LLM_PROVIDER=google
GOOGLE_API_KEY=...
```

The free tier covers **Flash and Flash-Lite only**, which is why both tiers
default to Flash; a Pro model needs billing attached.

Budget for the rate limit rather than the token cost. The quota that bites is
`GenerateRequestsPerMinutePerProjectPerModel`, and measured against a real key
it is **5 requests a minute**, not the 15 the docs suggest. An 11-agent pass is
35–45 requests, so it takes about twenty minutes of mostly waiting. Two things
make that bearable, and both are defaults:

- The two tiers use **different Flash ids**. The quota is counted per model, so
  sharing one makes the eleven agents queue behind each other for nothing.
- `LLM_MAX_RETRIES=10`. A rate-limited provider is a queue, not an error — but
  the client defaults (6 for Gemini, 2 for Claude) back off for about half the
  wait a free tier asks for and then give up, which fails the run and loses the
  work. Three agents died that way on the first pass.

**Claude.** From [console.anthropic.com](https://console.anthropic.com/settings/keys):

```bash
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

`MODEL_REASONING` (`claude-opus-5`) takes the analytical agents — forecasting,
pricing, reconciliation, scheduling, reorder. `MODEL_CONVERSATIONAL`
(`claude-sonnet-5`) takes the guest-facing, high-volume ones.

Either way, check the key and the request shape before setting thirteen agents
going:

```bash
restaurant-ai live-check     # one small call per tier, with token counts
restaurant-ai models         # what this key can actually see
```

`models` exists because model ids move — Gemini Flash went 3.0 to 3.7 inside a
year — and the `-latest` aliases are not safe to pin to; one of them resolved to
a deprecated model and returned a bare 404. It is the answer to "that id is
wrong", which is otherwise a mystery.

### What differs between the two

Everything above `kernel/llm.py` is provider-agnostic: the graph, the loop, the
tool dispatch and the approval gate all sit on LangChain's `BaseChatModel`. What
differs is confined to that one module, and both differences are 400s or
warnings rather than anything subtle:

- **Neither takes a `temperature`.** Claude Opus 5 and Sonnet 5 reject a request
  carrying one outright; Gemini 3 Flash uses fixed sampling and discards it,
  warning once per call. `LLM_TEMPERATURE` is unset by default for both, and is
  only correct against a model old enough to accept it.
- **Thinking is spelled differently.** Claude takes
  `LLM_THINKING=adaptive`. Gemini 3 dropped `budget_tokens` for a thinking
  *level*, so it takes `GOOGLE_REASONING_EFFORT=minimal|low|medium|high` and
  uses the model's own default when unset.

One thing that is *not* a difference, having been checked rather than assumed:
the google-genai SDK announces "AFC is enabled with max remote calls: 10" on
every call. Automatic function calling is absent from the actual request —
tools go as declarations, not callables — so the SDK does not execute the stub
functions the graph binds. Had it done so, every agent would have received
`"Tool execution is handled by the agent runtime."` as its tool result, and it
would have looked like a model problem rather than a wiring one.

Gemini's function declarations are an OpenAPI 3.0 subset, which is the usual
rough edge — nine tool arguments here are `str | None` and serialise as
`anyOf: [string, null]`. They convert to `nullable=True` correctly, and
`test_llm_wiring.py` asserts it for all thirty tools so a converter change
cannot break it quietly.

### Watching one agent think

```bash
restaurant-ai run-agent ordering --path model --transcript \
  --payload '{"guest_message": "any nut-free mains?"}'
restaurant-ai run-agent ordering --path deterministic
```

`--path` pins one run to one planner, so the model's choice can be held against
what the deterministic path would have done for the same restaurant on the same
day. Each run reports its turns and token counts, and `agent_run.model` records
which model answered — so what a day actually cost is a query, not an estimate.

---

## Putting your real menu in

Every number the agents produce is computed from the catalog — recipes explode
into ingredient demand, costs roll up into margins, margins into Irma's
classifications and Camelia's verdicts. Until the catalog is yours, it is all
demo data. The way in is a spreadsheet:

```bash
restaurant-ai menu-template menu.xlsx   # writes the fill-in workbook
# fill in the sheets — the ReadMe tab explains every column
restaurant-ai import-menu menu.xlsx --dry-run   # validate + cost, write nothing
restaurant-ai import-menu menu.xlsx             # load it
restaurant-ai import-menu menu.xlsx --replace-menu   # ...and retire dishes not in the file
```

The template's example rows are themselves a working import — run `import-menu`
on the untouched file to see the whole flow. Two properties worth knowing:

- **All-or-nothing.** Every problem is reported at once, each with its sheet
  and row, and nothing loads until the file is clean. A half-imported catalog
  is worse than none.
- **It ends with proof.** Every dish is costed through the same recipe
  explosion the agents use, and the import prints plate cost, margin and
  derived allergens per dish. A dish that cannot be costed aborts the import.

Re-importing the same file is a no-op; re-importing an edited one updates in
place. Prices, recipes and packs change — the file stays the source of truth.

---

## Connecting Telegram

Approvals are the first thing worth connecting, because they are how you stay in
control of five agents that can spend money or publish. And Telegram is the one
integration that needs **no hosting at all**.

1. Message [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token.
2. Message your new bot once, so a chat exists to send to.
3. Set `TELEGRAM_BOT_TOKEN`, `APPROVAL_CHANNEL=telegram`, and
   `APPROVAL_API_KEY` (any long random string — `python -c "import secrets;
   print(secrets.token_urlsafe(32))"`).
4. `restaurant-ai telegram-check` — confirms the token, names the bot, resolves
   `TELEGRAM_CHAT_ID` to whoever is on the other end, and sends a test card.

   Find the chat id by messaging the bot and reading `"chat":{"id": ...}` from
   `https://api.telegram.org/bot<token>/getUpdates`. It is around ten digits.
   The check resolves it to a name before sending, so a typo or a copied
   placeholder is caught while you are configuring rather than at the moment
   the first real approval fails to arrive.
5. `restaurant-ai telegram-listen` — takes decisions. Leave it running.

That is the whole setup. No public URL, no TLS certificate, no DNS: the listener
asks Telegram for updates rather than waiting to be called, so it works from a
laptop behind a router.

The webhook path is the right answer once the service has a public address —
point Telegram at `/approvals/telegram/callback` and set
`TELEGRAM_WEBHOOK_SECRET`. Telegram permits a webhook **or** polling, never
both; `telegram-listen` refuses rather than hanging if it finds one registered.

### Who is allowed to press the button

`TELEGRAM_CHAT_ID` is an allow-list, not just an address. A bot token is a
bearer credential and any chat the bot is in can press its buttons, so a press
from anywhere else is refused and logged. With no chat configured, nobody is
permitted — including you.

---

## The approval endpoints are closed by default

These all fail closed: **a missing secret means the endpoint refuses to serve,
not that it serves anyone.**

| Endpoint | Guard |
|---|---|
| `GET /approvals` | `APPROVAL_API_KEY` via `X-API-Key` |
| `POST /approvals/{id}/resolve` | `APPROVAL_API_KEY` |
| `POST /agents/{name}/run` | `APPROVAL_API_KEY` |
| `POST /approvals/telegram/callback` | `TELEGRAM_WEBHOOK_SECRET` |
| `POST /approvals/slack/interactivity` | `SLACK_SIGNING_SECRET` |
| `GET /health`, `/ready` | open — something has to be able to watch the service |

This is not hypothetical tidying. Before it, `GET /approvals` listed every
pending request with its id and the Telegram callback resolved any id it was
handed, both unauthenticated. Demonstrated against the running app: eleven
approvals listed and one approved as `not-the-owner`, with no credentials at
all. The gate is the platform's entire safety story, and it was worth exactly as
much as the endpoint recording the answer — which was nothing.

Slack's verifier had the same shape in miniature: it returned early when no
signing secret was set, on the reasoning that this was local development. In any
deployment that missed the line, it meant anyone could approve anything.

---

## The owner's daily brief

The phone is the UI. Every night at 23:55 — ten minutes after Camelia closes the
books — one message lands in the same Telegram chat where the approval cards
arrive: every department, what failed, and what needs you.

```
The Great Invention — daily brief, 2026-08-28

MONEY — Emil & Camelia
  revenue 4,169.70 · 179 covers · avg check 23.29
  COGS 33.0% · labour 37.0% · prime 71.0% · margin 29.0%
  Prime cost 70.5% is unsustainable.

SUPPLY — Rain & Suri
  41 ingredients tracked, 7 at/below reorder point
    Pandan leaf: 1.0d cover (on hand 108.80, on order 0.00)
  3 purchase order(s) out with suppliers
...
NEEDS YOU
  - Rain (Stock Tracking & Auto-Reorder Agent): 3 purchase order(s)
    totalling 5093.80 (value 5093.80)
```

`restaurant-ai brief` prints it on demand; `--send` delivers it to Telegram.
Two properties: each section reports what its agents actually see (supply is
Rain's own `perceive`, so the brief and the agents cannot disagree), and a
broken section degrades to one "unavailable" line rather than costing you the
other five at midnight.

---

## The live dashboard

For a screen rather than a phone: `GET /dashboard?key=<APPROVAL_API_KEY>` on the
API serves a self-contained dark operations view — KPI tiles with sparklines and
count-up deltas, a 14-day revenue chart and a prime-vs-labour chart with the 60%
target hairline (crosshair tooltips on both), the departments, every agent's
status, and a NEEDS YOU panel. It refreshes itself every 30 seconds.

No CDN, no build step — one HTML file, because it has to work on a laptop in a
restaurant with flaky wifi. Same key, same fail-closed posture as the rest of
the approval surface, and the page itself carries no data: everything arrives
via the authenticated fetch, so a leaked URL without the key shows an empty
shell. Chart colors are the validated dark-mode palette; animations respect
`prefers-reduced-motion`; a data table backs every chart.

---

## The system map

`GET /dashboard/map?key=<APPROVAL_API_KEY>` (or the **system map** link in the
dashboard header) draws the platform as a machine: the AI core at the centre,
the six departments ringed around it, the eleven agents fanned out past their
department, each with a live status dot.

Press any node. A department lists its agents; an agent opens the full picture —
what it does, when it runs, what its last run actually said, every tool it can
call with the approval-gated ones marked, and the operating brief it reasons
from, verbatim.

It is drawn from the registry, not from a description of it: `/dashboard/map/data`
reads the same `AgentSpec` objects and the same beat schedule the runtime
executes, so the map cannot show an agent the system does not have or a tool an
agent cannot call. Departments are told apart by position and label rather than
six hues, because a six-colour wheel cannot clear the colourblind checks — the
only colour carrying meaning is the status dot, and every dot has a text legend.

---

## Telling the restaurant what to do

The approvals chat used to run one way. The agents wrote, the brief arrived at
23:55, and the owner's entire vocabulary was two buttons. `restaurant-ai
telegram-listen` now takes both halves of the conversation.

**Ask it something** and it answers from the restaurant's own state:

```
you   how much chicken do we have?
you   what are today's numbers?
you   why did Rain order rice?
```

**Tell it to do something** and it works out whose job it is, shows you, and
asks before anything runs:

```
you   we're running low, sort the stock out

bot   That is Rain's job — Stock Tracking & Auto-Reorder Agent.
      Monitors real-time ingredient levels via POS-driven deductions and
      drafts purchase orders when stock hits its reorder threshold.
      Run Rain now?
      [ Run Rain ]   [ No ]

you   (presses Run Rain)

bot   Rain is on it…
bot   Rain: Refreshing reorder policies from recent usage, then drafting
      purchase orders for anything at or below the updated reorder points.
      The card above needs your approval before it happens.
```

```
/run <name>  run that agent now, no guessing  (e.g. /run rain)
/agents      who does what
/brief       tonight's brief, on demand
/pending     what is waiting for you
/help        all of the above
```

### Why it does not simply do as it is told

Three separate things stand between an instruction and a mistake, because the
owner finds out which agent was picked only *after* it has run.

**Routing is a judgement, so it is confirmed.** The router answers `QUESTION`,
`RUN <agent>` or `UNCLEAR` and nothing else, and it is told never to guess
between two agents — the owner would rather be asked than surprised. A misroute
then costs a wrong sentence on the screen rather than a wrong agent in the audit
trail. Answering `RUN` and then naming something that is not an agent is itself
treated as a misroute, not as licence to pick the nearest match.

**There is always a path that no model touches.** `/run rain` resolves the name
against the registry and starts that agent — no classifier, nothing to
misunderstand, and it still works when the model is rate-limited or unreachable.
A router that cannot answer routes to `UNCLEAR`, never to a coin-flip, and the
reply names this path.

**Being told is not the same as being done.** The agent runs its own graph with
its own tools behind the same approval gate as always: telling Rain to reorder
produces drafts and a card, not a purchase order. Every run reports what
actually happened — parked, finished, or failed — and a run that throws says so
with the error. Silence is never the answer to anything, because silence reads
exactly like success: if handling an update fails anywhere, the failure is sent
into the chat rather than only into the log.

### "No such command"

Almost never a missing command — a checkout that was pulled and an install that
was not, so the CLI keeps loading an older copy from `site-packages`.

```
restaurant-ai --version
```

```
restaurant-ai 0.1.0
running from  /home/you/Great-invention/src/restaurant_ai
commit        30ce02f  Merge pull request #11: say why nothing happened
```

The path is the answer: if it points inside your checkout, `git pull` is enough.
If it points into `site-packages`, the commit reads *unknown — this is an
installed copy*, and a pull changes nothing until you `pip install -e .` again.
The version string never moves between releases, so the commit is the part that
tells you whether this is today's code.

### When nothing happens

Silence from the bot is the one failure that tells you nothing: a working
restaurant with a quiet night and a dead listener look identical from a phone.

```
restaurant-ai doctor
```

checks every link in the chain and changes nothing, so it is safe to run at any
time — the database, the model, the token, the chat id, whether a webhook is
blocking polling, and, the one that explains most quiet bots, whether anything
is actually reading the chat.

```
The Great Invention — what is and is not working

  ok    database        reachable, 41 ingredients tracked
  ok    language model  google — gemini-3.5-flash
  ok    telegram bot    @Keanu007_Bot (Keanu)
  ok    webhook         none — long polling is available
  ok    approvals chat  Sharif (private, id 9988...)
  FAIL  listener        NOT RUNNING — nothing is reading the chat, and 4
                        message(s) are waiting unanswered

  listener: Start it with `restaurant-ai telegram-listen`, and leave it
  running — close that terminal and the bot goes deaf again.
```

The listener check works without any bookkeeping of its own: Telegram allows
exactly one `getUpdates` at a time and answers a second with 409 Conflict, so
the refusal *is* the proof that something is polling. An answer instead of a
conflict means nobody is, and whatever it hands back is the backlog of messages
that went unanswered. It is called without an offset, so it consumes nothing —
a real listener still sees every one of them.

Two silent failures it names that nothing else does: a `TELEGRAM_CHAT_ID` that
does not resolve (every message is then ignored on purpose, and ignoring is
supposed to look like this), and a network that blocks `api.telegram.org`, which
is indistinguishable from a bad token until something says which.

### The desk itself still cannot act

Answering is **read-only by construction, not by instruction**. The model that
answers questions is given no tools at all — not a gated tool, not a tool it is
told to avoid. A prompt saying "do not change anything" is a request; a model
with nothing bound to it cannot write to this restaurant whatever it decides to
do. Running an agent is a separate, confirmed path through the registry, not
something the answering model can reach.

Answers come from `assistant.build_snapshot` — every agent's own `perceive`
view, the same ones the brief reports and the agents plan from, so the desk and
the agent that acts cannot disagree about the state of the restaurant. One
broken view is a line in the answer, not a failure to answer.

The same allow-list guards asking, instructing and pressing, and an unknown chat
gets silence rather than a refusal — a reply would confirm the bot exists and
hand a stranger the restaurant's numbers.

`restaurant-ai ask "..."` is the question desk from a terminal, which is how it
is tested without Telegram.

---

## The operating rhythm

Celery beat, in the restaurant's own timezone (a `23:30` that fires at `07:30`
local would be silently, expensively wrong — so it is checked).

| Time | Agent |
|---|---|
| 06:00 | Dynamic Prep Forecaster |
| 07:00, 15:00 | Stock Tracking & Auto-Reorder |
| 09:30 | Supplier & Invoice |
| every 30 min | Reservation & Table Management |
| every 5 min, 11:00–23:00 | Order Routing & Pacing |
| hourly | Feedback & Reputation |
| 11:00 | Social Media & Content |
| Mon 09:00 | Dynamic Pricing & Menu Engineering |
| Wed 10:00 | Shift Scheduling |
| 23:30 | Bookkeeping & Reconciliation |
| 23:45 | Daily Performance |

Event-driven work runs on the same queue: a POS sale deducts stock, and a
deduction that trips a reorder point emits `stock.low`.

---

## Ingestion

A webhook endpoint is an unauthenticated door into a system that can spend
money. So:

- **HMAC over the raw bytes**, with a timestamp, compared in constant time. A
  signature captured off the wire does not stay valid forever.
- **Recorded before processed**, keyed on the provider's own event id. The
  uniqueness check is the database constraint, not a prior `SELECT` — two
  concurrent deliveries would both pass a read check.
- **Guarded deduction.** `stock_deducted_at` on the order line means a
  redelivered sale cannot deduct twice.

Verified against a running API and worker: posting the same order twice creates
one order, deducts 360 g of chicken once, and moves 4 g of belacan through the
sambal sub-recipe.

---

## Integrations

Every external system is a `Protocol` with an in-repo simulator. Agents depend
on the protocol, so going live on one vendor is one class and one environment
variable.

| Port | Real target | Simulator |
|---|---|---|
| `POSPort` | Square, Toast | a realistic service day: two peaks, channel mix, free-text dietary requests |
| `MessagingPort` | WhatsApp Business | natural-language bookings and orders |
| `ReviewsPort` | Google, social | ~18% needing escalation |
| `SupplierPort` | supplier EDI/email | short-delivers ~1 line in 5, over-charges ~1 in 6 |
| `SocialPort` | Meta, Instagram | records scheduled posts |
| `PayrollPort` | payroll provider | hours worked, with the over-runs real service produces |
| `BankPort` | merchant statements | card settlements a day late, platform payouts weekly net of commission |

The simulators are **seeded from the business date**, so a day replays
identically — which is what lets a simulated service day be a regression test
rather than a demo.

They are also **deliberately imperfect**. A supplier that always delivers in
full at the agreed price leaves the three-way match untested against the only
cases it exists for.

---

## Repository layout

```
src/restaurant_ai/
  domain/          pure logic — forecasting, inventory, costing, pricing,
                   scheduling, pacing, reconciliation, tables
  kernel/          the shared agent graph, registry, approval gate, audit
  agents/          the 11, in 6 department packages
  db/              SQLAlchemy models (53 tables) + the demo restaurant
  integrations/    ports + simulators
  api/             FastAPI: signed webhooks, agent control, approvals,
                   the dashboard and the system map
  worker/          Celery app, beat schedule, tasks
  approvals/       the gate: service, Slack, Telegram, the long-poll listener
  brief.py         the owner's nightly message
  assistant.py     the question desk — read-only, no tools bound
  simulation.py    a full service day through the real path
```

---

## Testing

558 tests, run on every push and pull request by
[`.github/workflows/ci.yml`](.github/workflows/ci.yml). `pytest` runs them all
against a real PostgreSQL with no API key and no network — if CI ever needs a
secret to pass, the deterministic path has regressed.

That includes the live-model path. `tests/test_live_reasoning.py` drives it
through a stub model that returns what the test tells it to and records what it
was shown, so a tool the model chose, the result it gets back, a two-step plan,
the iteration cap and the approval gate are all covered without a key. It cannot
check whether Claude chooses *well* — only that what it chooses is carried out
and that what comes back is true. The path had rotted to the point of being
unreachable precisely because nothing exercised it.

CI does four things beyond the suite: checks formatting (so `make fmt` and CI
cannot disagree), typechecks, replays a full simulated service day through the
CLI to prove the entry point a person actually types still works, and round
trips the migrations down to base and back up.

PostgreSQL rather than SQLite because the schema uses JSONB, partial constraints
and expression indexes that SQLite cannot express — an in-memory substitute
would be testing something other than what ships.

The end-to-end test simulates a whole service day and asserts the books balance.
Some things it has caught:

- The forecaster's trend factor compared window *totals*, so a window with more
  weekend days in it read as growth and one ending before the target date read
  as decline. Since you always forecast from history that stops before the day
  in question, that fired constantly.
- `StationLoad.is_bottleneck` compared the post-throttle queue against capacity —
  but the throttle holds the queue at exactly capacity, so the flag could never
  be true.
- The stock agent decided what to order from a snapshot taken *before* it
  recalculated the reorder points that snapshot was based on. It reported "all
  ingredients above their reorder points" while ten sat well below.
- Reconciliation matched delivery-platform takings against the daily card
  statement. Those settle weekly on the platform's own payout, so an entire
  day of delivery sales was reported unreconciled every night.
- The end-of-day report called a 34% prime cost "healthy" when labour was simply
  missing. Prime cost *is* COGS plus labour; reporting it without labour tells
  an owner their worst month was their best.
- The scheduler sized the roster from `forecast.total_covers`, which actually
  held forecast *dishes*. At a 1.4-dish basket that overstates the room by a
  third and staffs accordingly. The field is now `total_units`, with `covers`
  derived from the measured basket size.
- The roster had no daily hours ceiling. Two adjacent shifts are not an overlap
  and neither breaks the weekly cap, so one chef was handed 10:00–19:00 and then
  the 19:00–00:00 close — a fourteen-hour day.
- The pacing agent crashed the moment any KDS ticket existed — a single-column
  `select(...).scalars()` yields values, not rows, so its re-fire guard was
  doing `str.order_line_id`. Every other agent test ran against a quiet kitchen,
  so it was never reached. It runs every five minutes through service, so the
  pass would have stopped receiving tickets for the rest of the night. The run
  had also reported success while the tool underneath raised, which is why a
  run whose tools all fail is now marked failed and names them.

---

## Configuration

Everything is in `.env.example` and read in exactly one place (`config.py`).
The defaults run the whole platform simulated, so an empty `.env` works.

```bash
LLM_PROVIDER=fake            # fake | anthropic | google
LLM_THINKING=adaptive        # adaptive | disabled | off  (Anthropic only)
GOOGLE_REASONING_EFFORT=     # minimal | low | medium | high  (Gemini only)
APPROVAL_CHANNEL=none        # slack | telegram | none
SERVICE_LEVEL_Z=1.65         # 95% chance of not stocking out in a lead time
PRICE_CHANGE_MAX_PCT=0.10    # no single price move exceeds this
MIN_GROSS_MARGIN_PCT=0.55
APPROVAL_VALUE_THRESHOLD=250.00
```

`SERVICE_LEVEL_Z` is the one dial worth understanding: it is the
waste-versus-stockout trade-off. Raising it carries more stock and wastes more;
lowering it 86s dishes on a Friday night.

### Tuning the roster

Labour is the other half of prime cost, and `domain/scheduling.py` holds the
dials:

| Constant | Effect |
|---|---|
| `COVERS_PER_HOUR` | How many guests one person of a role handles. Raising it thins the roster |
| `MINIMUM_HEADCOUNT` | Who must be present whenever the doors are open, regardless of volume |
| `DISCRETIONARY_ROUNDING` | How much of a person's work must exist before a host, barista or porter is called in at all |
| `MIN_SHIFT_HOURS` / `MAX_SHIFT_HOURS` | Shift geometry — nobody comes in for ninety minutes |
| `MAX_DAILY_HOURS` | Ceiling on one person's day across every shift they hold |
| `OPEN_HOUR` / `CLOSE_HOUR` | Trading hours. The single biggest lever on a quiet day |

Roles are sized **hour by hour against the demand curve**, then packed into real
shifts by covering the busiest uncovered hour first. That produces the shape a
manager would draw by hand — an opening shift, extra bodies across the peaks, a
closing shift — instead of putting everyone on for the whole day.

The two settings that matter most are the least obvious. `DISCRETIONARY_ROUNDING`
exists because `ceil()` on a trickle of demand turns 0.13 of a host into a whole
one, which is what put a dedicated host, barista and porter on the floor for a
thirteen-hour day. And a quiet day *should* look bad: sixty covers across twelve
trading hours is unprofitable on labour whatever the roster does, and the right
lever is opening hours, not thinner staffing.

---

## What this pass does not do

Real vendor credentials or live integrations. A web or mobile UI — Slack,
Telegram and the CLI are the interfaces. Multi-location tenancy. Auth beyond
webhook signature verification. Production deployment manifests.

Each of the seven integration ports has exactly one implementation today, the
simulator. Writing the second one is the intended next step, and the protocol
is the contract it has to meet.
