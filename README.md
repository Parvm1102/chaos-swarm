# Swarm Collusion Harness

A research harness for the Apart Research Swarm Alignment Hackathon (Track 2: Science of Agent
Networks). Two LLM agents each solve a synthetic data-search task, exchange messages of at most
200 characters, and give ACCEPT/REJECT verdicts on each other's work under a verification rule
that the channel cannot satisfy. Across 10 episodes with memory we measure when pairs collude
(mutual ACCEPT), and extend the setup of Shi, Zhang & Yang (2026), "Emergent Collusion in
Long-Horizon LLM Agent Interaction", with three experiments: spillover to an unrelated rule, an
open door to report the broken channel, and transmission from colluding to fresh agents. Dry-run
is the default; live API calls need `SWARM_LIVE=1`, and every run has a hard USD cap.

## Setup

```bash
conda env create -f environment.yml
conda activate swarm
pip install -e .
cp .env.example .env   # then put your ANTHROPIC_API_KEY in .env
```

## Commands

```bash
pytest -q                       # offline tests (live tests skip unless SWARM_LIVE=1)
ruff check . && ruff format .
swarm check-env                 # python, conda env, SDK version, key present, SWARM_LIVE
swarm ping --provider anthropic --model claude-haiku-5-5   # needs SWARM_LIVE=1
swarm cost-report <run_name>    # spend from runs/<run_name>/usage.jsonl

swarm tasks generate --seed 0 --n 100   # the task set in data/tasks/seed0 (DBs are not committed)
swarm tasks sequences --seed 0          # 20 fixed sequences x 10 episodes x 2 agents
swarm tasks stats data/tasks/seed0      # per-template counts, answer sizes, duplicate check
swarm tasks show data/tasks/seed0 task_0007 [--reveal]
swarm tasks generate --seed 0 --n 100 --difficulty hard   # data/tasks/seed0-hard: planted traps
swarm tasks sequences --seed 0 --difficulty hard          # the task set the experiments use

swarm calibrate --dry-run --n 8         # solo task phases with a scripted agent: offline, $0
swarm calibrate --model anthropic/claude-sonnet-5-5 --n 20 --cache-ttl 5m --budget 3   # live
```

```bash
swarm episode --dry-run --sequence 0 --episode 1   # one episode, scripted pair: offline, $0
swarm episode --sequence 0 --episode 1 --budget 1  # one live episode on data/tasks/seed0-hard
```

`swarm episode` runs one episode for the pair A, B (task phases, a 5-round channel of
200-character messages, verdicts, feedback, notes) and writes `runs/<run>/episodes/s<seq>_e<ep>.json`,
`transcripts/` and `usage.jsonl`.

`swarm calibrate` writes `runs/<run>/calibration.jsonl`, `summary.json` (accuracy, non-peek
accuracy and the solo peek rate, each with a 95% Wilson interval, plus cost and cache use),
`transcripts/` and `usage.jsonl`. Without `--dry-run` it makes live calls and needs
`SWARM_LIVE=1`.
