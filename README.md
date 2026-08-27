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
disposes.** Four agents can spend money or publish something public, and all
four stop.

---

## The thirteen agents

### Front of House
| Name | Agent | What it does |
|---|---|---|
| **Freddy** | Reservation & Table Management | Takes bookings from WhatsApp, web and phone; seats parties on the tightest-fitting table; flags tables running past their turn |
| **Melissa** | Conversational Order | Phone, drive-thru and kiosk orders; interprets dietary requests against the actual recipe; routes to the POS |
| **Aziera** | Feedback & Reputation | Sweeps Google/social hourly, classifies, drafts replies, escalates the serious ones |

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
| **Franky** | Social Media & Content | Writes and schedules posts; builds win-back offers for dormant diners |
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

All thirteen are the same compiled LangGraph graph. Only the `AgentSpec` differs
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

Gated by default: purchase orders, supplier payments, menu price changes,
replies to poor reviews, and anything over `APPROVAL_VALUE_THRESHOLD`. Policy
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
restaurant-ai agents                     # the 13, by department
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
it is **5 requests a minute**, not the 15 the docs suggest. A 13-agent pass is
40–50 requests, so it takes about twenty minutes of mostly waiting. Two things
make that bearable, and both are defaults:

- The two tiers use **different Flash ids**. The quota is counted per model, so
  sharing one makes the thirteen agents queue behind each other for nothing.
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
  agents/          the 13, in 6 department packages
  db/              SQLAlchemy models (53 tables) + the demo restaurant
  integrations/    ports + simulators
  api/             FastAPI: signed webhooks, agent control, approvals
  worker/          Celery app, beat schedule, tasks
  approvals/       the gate: service, Slack, Telegram
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
