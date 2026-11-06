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
```
