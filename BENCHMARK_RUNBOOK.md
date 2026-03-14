# FeatureBench Shapes Benchmark — Runbook

How to run the shapes-augmented Claude Code agent against the FeatureBench benchmark suite.

## What We're Benchmarking

**FeatureBench** (ICLR 2026) is a benchmark suite with 200 tasks from 24 Python repos. The **lite split** has 30 tasks — this is what we run.

We compare two agents:
- **Baseline**: `claude_code` — stock Claude Code + Opus 4.5 solving tasks directly (leaderboard result: 20% resolved on lite)
- **Treatment**: `shapes_claude_code` — a shapes-bootstrapped Claude Code agent. It first analyzes the repo to create a `.shapes/` directory (architecture, patterns, constraints), then a fresh Claude Code session solves the task using the `shapes` CLI to query relevant context on demand.

**Scoring**: A task is "resolved" if ALL fail-to-pass (F2P) tests pass. The resolved rate is the primary metric.

## Architecture

```
┌─────────────────────────────────────────────┐
│  fb infer (orchestrator on host)            │
│  - Spawns Docker containers per task        │
│  - Runs 6 tasks in parallel (n_concurrent)  │
│  - Writes results to output.jsonl           │
└───────────┬─────────────────────────────────┘
            │ For each task:
            ▼
┌─────────────────────────────────────────────┐
│  Docker container (per-task isolation)       │
│                                             │
│  Phase 1: Bootstrap (~4 min)                │
│  - shapes-cli binary copied in              │
│  - Claude Code session analyzes repo        │
│  - Creates .shapes/ with 20-40 shapes       │
│    and 10-20 constraints                    │
│                                             │
│  Phase 2: Task solving (~5-30 min)          │
│  - Fresh Claude Code session                │
│  - Reads CLAUDE.md (shapes CLI docs)        │
│  - Runs `shapes tree`, `shapes show <id>`   │
│  - Solves the task                          │
│                                             │
│  Phase 3: Cleanup                           │
│  - .shapes/ and CLAUDE.md removed           │
│  - git diff extracted as patch              │
└─────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────┐
│  fb eval (evaluator)                        │
│  - Applies patches to clean containers      │
│  - Runs F2P and P2P test suites             │
│  - Produces report.json                     │
└─────────────────────────────────────────────┘
```

## Prerequisites

### 1. Fork and Branch

All custom code lives on the `shapes-benchmark` branch of our fork:
```bash
git clone git@github.com:snowmead/FeatureBench.git
cd FeatureBench
git checkout shapes-benchmark
```

Upstream: `https://github.com/LiberCoders/FeatureBench.git`

### 2. Shapes CLI Binary

The shapes CLI must be cross-compiled for linux/amd64 (statically linked via musl):

```bash
cd ~/opt/shapes-cli
rustup target add x86_64-unknown-linux-musl
cargo build --release --target x86_64-unknown-linux-musl
# Binary at: target/x86_64-unknown-linux-musl/release/shapes-cli (~2.8 MB)
```

### 3. API Keys

You need:
- **Anthropic OAuth token** (`sk-ant-oat01-...`) — used for Claude Code sessions
- **HuggingFace token** (`hf_...`) — used to download the FeatureBench dataset

## Server Setup

### Provider: Hetzner Cloud

- **Instance**: CPX62 (16 shared vCPU AMD, 32 GB RAM, 640 GB SSD)
- **Region**: Singapore (hil)
- **OS**: Ubuntu 22.04+
- **Cost**: ~$0.137/hr (~$3.29/day)
- **Important**: Delete the server when done to stop billing!

### Provisioning

1. Create server on [Hetzner Cloud Console](https://console.hetzner.cloud/)
2. Add your SSH key during creation
3. SSH in: `ssh root@<SERVER_IP>`

### Server Installation

```bash
# Install Docker
curl -fsSL https://get.docker.com | sh

# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH=/root/.local/bin:$PATH

# Clone the fork
git clone https://github.com/snowmead/FeatureBench.git
cd FeatureBench
git checkout shapes-benchmark

# Pre-pull Docker images for the lite split (saves time during benchmark)
uv run fb pull --mode lite
```

### Copy Files to Server

From your local machine:
```bash
# Copy shapes binary
scp ~/opt/shapes-cli/target/x86_64-unknown-linux-musl/release/shapes-cli root@<SERVER_IP>:/root/shapes-cli

# Copy config (contains API keys — never commit this!)
scp config.toml root@<SERVER_IP>:/root/FeatureBench/config.toml
```

### config.toml

This file lives at `/root/FeatureBench/config.toml` on the server (and locally, but is in `.gitignore`). Key sections:

```toml
[env_vars]
HF_ENDPOINT=""
HF_TOKEN="hf_..."                    # HuggingFace token (REQUIRED — empty string causes errors)

[infer]
download_cache_dir="/root/FeatureBench/download_cache"  # On server; locally use local path

[infer_config.shapes_claude_code]
ANTHROPIC_API_KEY = "sk-ant-oat01-..."   # OAuth token (auto-routed to CLAUDE_CODE_OAUTH_TOKEN)
ANTHROPIC_BASE_URL = ""
CLAUDE_CODE_VERSION = ""
SHAPES_BINARY_PATH = "/root/shapes-cli"  # On server; locally use local path
SHAPES_BOOTSTRAP_TIMEOUT = "1800"        # 30 min bootstrap timeout

[infer_config.claude_code]
ANTHROPIC_API_KEY = "sk-ant-oat01-..."   # Same OAuth token for baseline runs
ANTHROPIC_BASE_URL = ""
CLAUDE_CODE_VERSION = ""
```

**OAuth token routing**: Tokens starting with `sk-ant-oat` are automatically detected and set as `CLAUDE_CODE_OAUTH_TOKEN` (not `ANTHROPIC_API_KEY`). This is handled in `claude_code.py` lines 210-214.

**Model ID**: Use `claude-opus-4-5` (not `claude-opus-4-5-20250514`) — the versioned ID doesn't work with OAuth.

## Running the Benchmark

### Inference (Task Solving)

Run in a tmux session so it survives disconnects:

```bash
tmux new-session -s benchmark

export PATH=/root/.local/bin:$PATH
cd /root/FeatureBench

uv run fb infer \
  --config-path config.toml \
  --agent shapes_claude_code \
  --model claude-opus-4-5 \
  --split lite \
  --no-gpu \
  --timeout 1800 \
  --n-concurrent 6 \
  2>&1 | tee /root/benchmark.log
```

**Parameters**:
- `--agent shapes_claude_code` — our custom agent (or `claude_code` for baseline)
- `--model claude-opus-4-5` — the model for both bootstrap and task solving
- `--split lite` — 30-task subset
- `--no-gpu` — these tasks don't need GPUs
- `--timeout 1800` — 30 min per task (agent execution only; bootstrap has its own timeout)
- `--n-concurrent 6` — parallel tasks (tuned for 16 vCPU / 32 GB RAM)

**Output**: `runs/<timestamp>/output.jsonl` + per-task logs in `runs/<timestamp>/run_outputs/<task_id>/attempt-1/`

### Resuming Interrupted Runs

If the run is interrupted, use `--resume` to skip completed tasks:

```bash
uv run fb infer \
  --config-path config.toml \
  --agent shapes_claude_code \
  --model claude-opus-4-5 \
  --split lite \
  --no-gpu \
  --timeout 1800 \
  --n-concurrent 6 \
  --resume \
  2>&1 | tee -a /root/benchmark.log
```

### Evaluation

After inference completes:

```bash
uv run fb eval \
  --config-path config.toml \
  --predictions-path runs/<timestamp>/output.jsonl \
  --split lite \
  --no-gpu \
  --n-concurrent 6 \
  2>&1 | tee /root/eval.log
```

**Output**: `runs/<timestamp>/report.json` with resolved rate, F2P pass rate, and per-task results.

## Monitoring Progress

```bash
# How many tasks completed
wc -l runs/<timestamp>/output.jsonl

# Running containers
docker ps --format "{{.Names}}" | wc -l

# Completed tasks with patch sizes
grep "Successfully processed" runs/<timestamp>/run_outputs/*/attempt-1/run.log \
  | sed 's|.*/run_outputs/||;s|/attempt-1.*patch: | => |;s| chars.*||'

# Failures
grep "Error processing" runs/<timestamp>/run_outputs/*/attempt-1/run.log

# Bootstrap health (should see NO "exited with code 1" after tee fix)
grep "Bootstrap exited" runs/<timestamp>/run_outputs/*/attempt-1/run.log

# Shapes created per task
grep "Captured.*chars" runs/<timestamp>/run_outputs/*/attempt-1/run.log

# Bootstrap depth (shapes + constraints count)
grep "Bootstrap results" -A50 runs/<timestamp>/run_outputs/*/attempt-1/run.log | grep "promoted" | wc -l
```

## Reading Results

### report.json

```python
import json
with open("runs/<timestamp>/report.json") as f:
    data = json.load(f)

a = data["attempt_1"]
print(f"Resolved: {a['resolved_instances']}/{a['total_instances']} ({a['resolved_rate']:.1%})")
print(f"F2P pass rate: {a['pass_rate']:.1%}")

# Resolved task IDs
for r in a["resolved_ids"]:
    inst = r.split("/run_outputs/")[1].split("/")[0]
    print(f"  {inst}")
```

### Per-Task Logs

Each task has three log files:
- `run.log` — high-level status (bootstrap time, agent time, patch size)
- `infer.log` — full agent transcript (all tool calls, API responses)
- `claude_code_stream_output.jsonl` — raw Claude Code stream output

## Key Files

| File | Location | Purpose |
|------|----------|---------|
| `shapes_claude_code.py` | `featurebench/infer/agents/` | Custom agent code (bootstrap + CLI-as-tool) |
| `claude_code.py` | `featurebench/infer/agents/` | Base Claude Code agent (reference) |
| `models.py` | `featurebench/infer/` | Agent registration (`SHAPES_CLAUDE_CODE` enum) |
| `config.toml` | repo root (gitignored) | API keys, paths, agent config |
| `shapes-cli` | `/root/shapes-cli` on server | Statically linked Linux binary |

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| `Illegal header value b'Bearer '` | Empty `HF_TOKEN` in config.toml | Add a valid HuggingFace token |
| Bootstrap exits with code 1 | `/agent-logs/` dir missing (v1 bug) | Fixed in v2 — `mkdir -p /agent-logs` before bootstrap |
| `apiKeySource: "none"` in logs | Normal with OAuth tokens | Not a problem — Claude Code works fine via `CLAUDE_CODE_OAUTH_TOKEN` |
| Agent timeout (-1 exit code) | Task too hard / 1800s not enough | Normal — some tasks are genuinely hard |
| Model version error | Using versioned model ID with OAuth | Use `claude-opus-4-5` not `claude-opus-4-5-20250514` |

## Version History

| Version | Date | Resolved (raw) | Resolved (valid*) | Key Changes |
|---------|------|----------------|-------------------|-------------|
| Baseline | Leaderboard | 20.0% (6/30) | 20.0% | Stock Claude Code + Opus 4.5, no shapes |
| v1 | 2026-03-13 | 15.4% (4/26) | 15.4% | Shapes YAML in system prompt (~15k chars/turn), shallow bootstrap |
| v2 | 2026-03-13 | 13.8% (4/29) | 20.0% (4/20) | CLI-as-tool, deeper bootstrap, tee fix. 9 tasks rate-limited. |
| v3 | 2026-03-14 | 24.1% (7/29) | **38.9% (7/18)** | Continuous consultation, compact system prompt, no-TODO bootstrap. 11 tasks rate-limited. |

*Valid = excluding tasks where API rate limits caused bootstrap failure (no shapes context at all)

**v3 gained 3 new resolves over v2** (mlflow test_validation, mlflow test_span, pandas test_list_accessor) with 0 regressions.

## Cleanup

**Delete the Hetzner server when done** to stop billing ($0.137/hr = $3.29/day):

1. Hetzner Cloud Console -> Servers -> Select server -> Delete
2. Or via CLI: `hcloud server delete <server-id>`
