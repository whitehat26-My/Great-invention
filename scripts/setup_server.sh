#!/usr/bin/env bash
#
# Set up a fresh Ubuntu server to run the restaurant.
#
# Nothing here is specific to one provider. It has been written against
# Oracle's free tier and works the same on a Malaysian VPS, a Hetzner box or
# a Raspberry Pi: what it needs is Ubuntu, root, and Docker.
#
# Everything here happens on the server, after `ssh ubuntu@<ip>`. It is
# idempotent: run it again after a failure, or after changing .env, and it picks
# up where it left off rather than starting over.
#
# It stops before it can do harm. A .env it had to create is a .env with no
# token in it, and starting four processes against that gives a deaf bot and a
# confusing afternoon — so it writes the file, says what to put in it, and
# waits to be run again.
#
#   bash scripts/setup_server.sh
#
set -euo pipefail

REPO="https://github.com/whitehat26-My/Great-invention.git"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TZ_WANTED="${TZ:-Asia/Kuala_Lumpur}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31m!!  %s\033[0m\n\n' "$*" >&2; exit 1; }

# --- 1. What machine is this? ------------------------------------------------
say "Checking the machine"
note "$(uname -s) $(uname -m), $(nproc) core(s), $(free -g 2>/dev/null | awk '/^Mem:/{print $2}' || echo '?')GB RAM"
if [[ "$(uname -m)" == "aarch64" ]]; then
  note "ARM. Every image here has an arm64 build, but the first one takes longer to make."
fi
[[ "$(uname -s)" == "Linux" ]] || die "This script is for the Linux server, not your own machine."

# --- 2. Time ----------------------------------------------------------------
# Wrong here and the nightly close fires at the wrong hour, which nobody
# notices until the books are a day out.
# Not fatal. A clock that cannot be set is a nightly close at the wrong hour —
# worth saying loudly, and no reason at all to abandon the install. `set -e`
# would have done exactly that on any host without systemd.
if [[ "$(timedatectl show -p Timezone --value 2>/dev/null || echo)" != "$TZ_WANTED" ]]; then
  say "Setting the clock to $TZ_WANTED"
  if sudo timedatectl set-timezone "$TZ_WANTED" 2>/dev/null; then
    note "Done."
  else
    note "Could not set it (no systemd here?). Carrying on."
    note "Set TZ=$TZ_WANTED in .env so the schedule is right even if the host clock is not."
  fi
fi
note "Local time is now $(date '+%Y-%m-%d %H:%M %Z')"

# --- 3. Docker --------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  say "Installing Docker and git (a few minutes, mostly downloading)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io docker-compose-v2 git
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER"
  note "You have been added to the docker group."
  note "That only applies to a NEW login, so: log out, log back in, run this again."
  exit 0
fi
if ! docker ps >/dev/null 2>&1; then
  die "Docker is installed but will not talk to you. Log out and back in (the group
    membership from the last run needs a fresh login), then run this again."
fi
note "Docker $(docker --version | awk '{print $3}' | tr -d ,) is up"

# --- 4. The code ------------------------------------------------------------
if [[ ! -f "$HERE/docker-compose.yml" ]]; then
  die "Run this from inside the cloned repository:
    git clone $REPO && cd Great-invention && bash scripts/setup_server.sh"
fi
cd "$HERE"
say "Updating the code"
git pull --ff-only || note "Could not fast-forward — carrying on with what is here."

# --- 5. Settings ------------------------------------------------------------
# Written once and never overwritten: this file holds the only secrets on the
# machine, and a setup script that clobbers them is a setup script nobody runs
# twice.
if [[ ! -f .env ]]; then
  say "Writing a .env for you to fill in"
  cat > .env <<'ENV'
# --- Who this is ---
RESTAURANT_NAME=Restoran Suriani
TZ=Asia/Kuala_Lumpur

# --- The bot ---
# From BotFather. Paste it here; never into a chat.
TELEGRAM_BOT_TOKEN=
# Only this chat may ask, instruct or approve. Everyone else is ignored in silence.
TELEGRAM_CHAT_ID=
APPROVAL_CHANNEL=telegram

# --- The dashboard ---
# Any long random string. The link carries it, so treat the link as a password.
APPROVAL_API_KEY=

# --- The models ---
# Scheduled agents run free on this machine; nobody is waiting on them.
LLM_PROVIDER=ollama
OLLAMA_MODEL_REASONING=hermes3:latest
OLLAMA_MODEL_CONVERSATIONAL=hermes3:latest
# The owner's questions go somewhere fast. Without this the chat is minutes slow
# on a machine with no graphics card, which reads as a bot that has died.
LLM_PROVIDER_INTERACTIVE=anthropic
ANTHROPIC_API_KEY=
MODEL_CONVERSATIONAL=claude-haiku-4-5

# --- The database ---
POSTGRES_PASSWORD=
ENV
  chmod 600 .env
  cat <<'NEXT'

    Fill in the blanks in .env, then run this script again:

      nano .env

    You need four things:
      TELEGRAM_BOT_TOKEN   BotFather -> /token
      TELEGRAM_CHAT_ID     message the bot, then: restaurant-ai telegram-check
      APPROVAL_API_KEY     any long random string of your own
      POSTGRES_PASSWORD    any long random string of your own

    ANTHROPIC_API_KEY is optional but strongly advised: without it the chat runs
    on this machine's processor and takes minutes to answer.

NEXT
  exit 0
fi

missing=()
for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID APPROVAL_API_KEY POSTGRES_PASSWORD; do
  grep -qE "^${key}=.+" .env || missing+=("$key")
done
if (( ${#missing[@]} )); then
  die "These are still blank in .env: ${missing[*]}
    Fill them in (nano .env) and run this again. Starting without them gives a
    deaf bot and a dashboard that refuses to serve, with nothing saying why."
fi
note ".env looks complete"

# --- 6. Build and start -----------------------------------------------------
say "Building and starting (the first build takes a while — it compiles wheels)"
docker compose up -d --build

say "Waiting for the database to answer"
for _ in $(seq 1 60); do
  if docker compose exec -T postgres pg_isready -U restaurant >/dev/null 2>&1; then
    note "Postgres is up"; break
  fi
  sleep 2
done

# --- 7. Say what is true ----------------------------------------------------
say "What is and is not working"
docker compose exec -T api restaurant-ai doctor || true

cat <<'DONE'

    If the listener line is green, message the bot — it should answer.

    Still to do, in this order:
      1. Load the menu:
           docker compose exec api restaurant-ai start-real --yes \
             --menu menu/the-great-invention-menu.xlsx
      2. See what each agent still needs:
           docker compose exec api restaurant-ai readiness
      3. Optional, for free agent inference on this machine:
           curl -fsSL https://ollama.com/install.sh | sh
           ollama pull hermes3
         Slow here — no graphics card — which is why the chat goes to Claude.

    Back up the database nightly. It is the only thing on this machine that
    cannot be rebuilt from the repository:
      docker compose exec -T postgres pg_dump -U restaurant restaurant_ai | gzip > backup.sql.gz

DONE
