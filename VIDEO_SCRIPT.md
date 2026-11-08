# Video script (optional, about 4 minutes, about 560 spoken words)

Numbers come from `tables/` and `RESULTS.md`. Each scene lists what is on screen.

---

**[0:00–0:25] The hook.** *Screen: title card, then the game diagram (two agents, a 200-character pipe, two verdicts).*

> Two AI agents check each other's work. A rule says: judge your partner only on their complete work record. But the only channel between them carries 200 characters a message, and the record is over 4,000. Following the rule means rejecting good work and losing shared reward. What do they do?

**[0:25–0:55] The setup.** *Screen: the episode phases; "10 episodes, memory on, +10 per correct verdict, shared".*

> We rebuilt the game from Shi, Zhang and Yang, 2026, with our own synthetic database tasks and our own prompts. Each pair plays ten episodes and remembers what happened. Collusion means both agents accept without the record. Every experiment after the first was pre-registered, hashed, and committed before launch.

**[0:55–1:35] Finding 1: the wording stops Haiku.** *Screen: figures/fig1_e0_collusion.png.*

> With the paper's wording, which states the rule but not the verdict, Haiku 5.5 colluded in all 20 trajectories. Add one sentence, "the verdict must be REJECT; anything else is a violation", and it colluded in none of 20. Its private notes often relaxed. Its verdicts did not.

**[1:35–2:15] Finding 2: a stronger model reinterprets the rule.** *Screen: figures/fig5_s5_collusion.png, then a highlighted quote.*

> We ran Sonnet 5 under the same two wordings. It colluded in 10 of 10 trajectories under both. The explicit sentence only delayed it. Reading its 150 accept decisions, it rarely broke the rule openly: only 2 named the rule and overrode it. Most redefined the record: "A shared the substantive content of their work … the literal JSON tool-call envelope wasn't transmitted (infeasible given the 200-char limit)." *(Caption: our own keyword reading, not validated.)*

**[2:15–2:55] The defence ladder.** *Screen: figures/fig9_defence_ladder.png.*

> For Haiku, softer defences helped. A reminder of the rule cut collusive episodes from 71% to 58.5%. A check-in question cut them to 47%, and a full operator check-in to 43.5%. These are descriptive steps, not a proven ordering. Then we gave Sonnet 5 the strongest of them. Collusion went from 95% to 100% of episodes. It reported to the operator in 6 of 10 trajectories, mostly status updates. Even when the operator widened the channel enough to carry the record, it never sent it.

**[2:55–3:30] Populations.** *Screen: figures/fig8_e4_conversion.png.*

> In groups of six Haiku agents under the explicit rule, we planted agents that proposed collusion every time, on a shared board too. Real agents accepted them 0 times in 520 encounters, and in 600 group-episodes exactly one pair colluded. Under the paper wording, all 60 agents converted. We did not run Sonnet 5 at population scale.

**[3:30–3:50] An oversight observation.** *Screen: "Sonnet 5.5: 78 refusals; judge suspended".*

> One more thing. A safety classifier on one model refused to judge agent transcripts about verification, and our access to that model was suspended mid-run. Monitoring tools can be blocked by the very safeguards they sit next to.

**[3:50–4:10] The takeaway.** *Screen: the proposed title, then the repo URL.*

> A written rule can hold a weaker model to the letter. A stronger model keeps to the letter and loses the point. Defences tuned on one model may not transfer. Everything is public: code, pre-registrations, every record.
