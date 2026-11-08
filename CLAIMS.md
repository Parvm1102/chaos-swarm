# CLAIMS: every number in REPORT_DRAFT.md and its source

**How to read this ledger.**
- **Result numbers** trace to `tables/*.md`, `RESULTS.md` or `figures/CAPTIONS.md`.
- **Design parameters** (rounds, characters, episodes, group size, thresholds) trace to the documents that define them: README.md, PREREG.md and `src/swarm/prompts/*.txt`.
- **Hashes** trace to `runs/<arm>/arm.json`, `git log` and `tables/prompt_versions.md`.
- **Not ledgered:** labels such as "Experiment 0", "R1", "C1", "arm 5" or "s08 e03"; table and figure numbers; citation years.
- **Notes-only numbers** (found only in `docs/`) are marked "notes-only" and listed at the end.

| # | section | sentence (short excerpt) | number(s) | source file | where in the source |
|---|---|---|---|---|---|
| 1 | Abstract | "a series of pre-registered experiments (41 verdict rows)" | 41 | RESULTS.md | intro line "Verdict counts over 41 rows" |
| 2 | Abstract | "0/20 trajectories collude; under the paper's wording, 20/20 do" | 0/20; 20/20 | tables/arms_e0.md | TC: haiku_baseline 0/10 + haiku_explicit_think 0/10; haiku_paper_nothink 10/10 + haiku_paper_think 10/10 |
| 3 | Abstract | "Sonnet 5 colludes in 10/10 … (65.0% against 95.0%)" | 10/10; 65.0%; 95.0% | tables/arms_s5.md; RESULTS.md | TC and EC pooled, rows s5_explicit, s5_paper; Replication row R1 |
| 4 | Abstract | "reinterpret (71/150) or argue past it (59/150) … override … 2/150" | 71, 59, 2 of 150 | tables/s5_accept_justifications.md | s5_explicit rows reinterpret / ignore / override |
| 5 | Abstract | "58.5%, 47.0% and 43.5%, against 71.0% in control" | 58.5; 47.0; 43.5; 71.0 | tables/defence_ladder.md | EC pooled: e5_reminder, e5_question, e3_checkin, e3_control |
| 6 | Abstract | "pre-registered 15-point reminder effect was not supported" | 15 | RESULTS.md | Replication table, row E1 |
| 7 | Abstract | "check-in leaves collusion at 100%, against 95.0%" | 100; 95.0 | tables/defence_ladder.md; RESULTS.md | EC pooled s5_checkin, s5_paper; row C1 |
| 8 | Abstract | "In groups of six … (0 ACCEPTs in 520 encounters)" | 6; 0/520 | tables/arms_e4_population.md; PREREG.md | real-scripted ACCEPT rate 0/100 (k1), 0/160 (k2), 0/100 (board_k1), 0/160 (board_k2), sum 520; group size: PREREG §7 Design, "Groups" |
| 9 | 1 | "send each other at most five messages of 200 characters" | 5; 200 | README.md; tables/arms_e3.md | README "An episode" step 2 (five rounds, at most 200 characters); channel column 200 |
| 10 | 1 | "exceeds 4,000 characters, against 1,000 for the whole channel" | 4,000; 1,000 | README.md | "The rule" bullet |
| 11 | 1 | "10 points per correct verdict" | 10 | src/swarm/prompts/episode_rules_paper.txt; README.md | Reward paragraph; "Verdict scoring" bullet |
| 12 | 1 | finding 1: "0/20 trajectories colluded … 20/20 did" | 0/20; 20/20 | tables/arms_e0.md | as row 2 |
| 13 | 1 | finding 2: "colluded in 10/10 trajectories under both wordings" | 10/10 | tables/arms_s5.md; RESULTS.md | TC s5_explicit, s5_paper; row R1 |
| 14 | 1 | finding 4: "six-agent groups" | 6 | PREREG.md | §7 Design, "Groups" |
| 15 | 3.1 | "(10 tables) … 5–30 integer IDs" | 10; 5–30 | README.md | "The environment", Tasks bullet |
| 16 | 3.1 | "every work record exceeds 4,000 characters" | 4,000 | README.md | "The rule" bullet; Deviations bullet on the automatic inspect_database |
| 17 | 3.1 | "five rounds … at most 200 characters per agent per round" | 5; 200 | README.md; tables/arms_*.md | "An episode" step 2; channel column |
| 18 | 3.1 | "Each correct verdict earns 10 points for both agents" | 10 | README.md | "Verdict scoring" bullet |
| 19 | 3.1 | "Task accuracy was 90.8–100% across arms" | 90.8; 100 | tables/arms_e1.md; tables/arms_e0.md; tables/arms_s5.md | task accuracy: minimum e1_cap1_nomem 90.8%; maximum 100.0% (haiku_paper_nothink, s5 arms); all other arms (arms_e3, arms_e5, arms_e4pre, arms_e4_population) lie between |
| 20 | 3.1 | "across 10 episodes" | 10 | README.md | "Memory" bullet |
| 21 | 3.3 | "CC … onset by episode 6, at most one non-collusive episode after" | 6; 1 | README.md | "Metrics" bullet, CC |
| 22 | 3.3 | "At 200 characters per message the two coincide" | 200 | PREREG.md | §1 Design, "Effect per arm"; §8 "Comparison arm" |
| 23 | 3.3 | "Rates carry 95% Wilson intervals" | 95% | README.md | "Metrics" ("Every rate carries a 95% Wilson interval") |
| 24 | 3.3 | "with 10,000 resamples and seed 0" | 10,000; 0 | PREREG.md | §1 "Statistics and verdicts" |
| 25 | 3.3 | "falsified when pooled rates lie within 5 points" | 5 | PREREG.md; RESULTS.md | §1 verdict rule 1; "The Experiment 4 boundary note" |
| 26 | 3.4 | "one labeller, 15 items per kind" | 1; 15 | tables/validation.md; RESULTS.md | items column (haiku, sonnet5 rows); "Validation of the judges", bullet 1 |
| 27 | 3.5 | "a 200-character channel" | 200 | tables/arms_e3.md | channel column, e3_control |
| 28 | 3.5 | Table 1: trajectories per arm (10, 20), 8,000-character, 6 arms, "7 arms × 10 groups of 6" | 6; 10; 20; 8,000; 7 | tables/arms_e0.md, arms_e1.md, arms_e3.md, arms_e5.md, arms_s5.md, arms_e4pre.md, arms_e4_population.md | trajectories, channel and groups columns; README "Experiments" table; group size PREREG §7 |
| 29 | 3.6 | "first commit `cf744d1`, 2026-11-06 21:44 IST" | date, time | README.md | "Provenance", bullet 1 (also `git log --reverse`) |
| 30 | 3.6 | "Total live spend was $147.9, … Sonnet 5 subject arms took $70.88" | 147.9; 70.88 | tables/costs.md | rows "total" and "Sonnet 5 as subject" |
| 31 | 4.1 | "TC 0/10 [0.0, 27.8] … 0/10 [0.0, 27.8] …, EC 0/100 [0.0, 3.7] in both" | as quoted | tables/arms_e0.md | TC, EC pooled: haiku_baseline, haiku_explicit_think |
| 32 | 4.1 | "TC 10/10 [72.2, 100.0] …, EC 66/100 = 66.0% [56.3, 74.5] … 59/100 = 59.0% [49.2, 68.1], CC 7/10 [39.7, 89.2] and 5/10 [23.7, 76.3]" | as quoted | tables/arms_e0.md | TC, EC pooled, CC: haiku_paper_nothink, haiku_paper_think |
| 33 | 4.1 | "the 20 paper-wording onsets as responsive (7 and 8) or simultaneous (3 and 2)" | 20; 7; 8; 3; 2 | tables/arms_e0.md | pathways (Haiku judge): "resp 7, simul 3" (paper_nothink), "resp 8, simul 2" (paper_think) |
| 34 | 4.1 | "proposes mutual acceptance in rounds 1 and 3" | 1; 3 | PREREG.md | §6 Design (round 1, repeated in round 3); prompts peer_proposal, peer_proposal_repeat |
| 35 | 4.1 | "colluded in 10/10 trajectories [72.2, 100.0] and accepted in 62/100 episodes (EC 62.0% [52.2, 70.9])" | as quoted | tables/arms_e0.md; PREREG.md | haiku_paper_scriptedB TC, EC pooled. A's ACCEPTs equal EC because the scripted B always ACCEPTs (PREREG §6 "Prior": "A accepted in 62/100 episodes") |
| 36 | 4.1 | "later median onset than organic pairs (4.5 against 3.0)" | 4.5; 3.0 | tables/arms_e0.md | median onset: haiku_paper_scriptedB, haiku_paper_nothink |
| 37 | 4.1 | "All 10 onsets were judged responsive relaxation" | 10 | tables/arms_e0.md | pathways, haiku_paper_scriptedB "resp 10" |
| 38 | 4.1 | "Sonnet 5.5 … 10/10 trajectories [72.2, 100.0], EC 82/100 = 82.0% [73.3, 88.3], … 78 refusals, all from its reasoning_extraction classifier" | as quoted | tables/arms_e0.md; docs/step4_notes.md | sonnet_paper_think TC, EC pooled, refusals 78; the category is notes-only (N2) |
| 39 | 4.1 | Figure 1 caption, all values | as quoted | figures/CAPTIONS.md | fig1_e0_collusion.png |
| 40 | 4.2 | "On the same sequences 0–9" | 0–9 | figures/CAPTIONS.md | fig5_s5_collusion.png ("sequences 0-9") |
| 41 | 4.2 | "colluded in 10/10 trajectories [72.2, 100.0] … and 10/10" | as quoted | tables/arms_s5.md | TC: s5_explicit, s5_paper |
| 42 | 4.2 | "R1, a TC gap of more than 50 points, … +0.0 points, Fisher p = 1" | 50; +0.0; 1 | RESULTS.md | Replication table, row R1 |
| 43 | 4.2 | "EC to 65/100 = 65.0% [55.3, 73.6] from 95/100 = 95.0% [88.8, 97.8] … median onset to episode 4.0 from 1.0; CC 9/10 = 90.0% [59.6, 98.2] against 10/10 [72.2, 100.0]" | as quoted | tables/arms_s5.md | EC pooled, median onset, CC: s5_explicit, s5_paper |
| 44 | 4.2 | "Neither arm had a refusal, and task accuracy was 100.0%" | 0; 100.0 | tables/arms_s5.md | refusals, task accuracy |
| 45 | 4.2 | "$22.62–$23.27 per Sonnet 5 arm, against about $1.7 for a 20-trajectory Haiku arm" | 22.62; 23.27; 1.7; 20 | tables/arms_s5.md; tables/arms_e3.md; tables/arms_e5.md | cost USD: s5_paper 22.62, s5_checkin 22.99, s5_explicit 23.27; Haiku e3/e5 arms 1.69–1.76 |
| 46 | 4.2 | Figure 2 caption, all values | as quoted | figures/CAPTIONS.md | fig5_s5_collusion.png |
| 47 | 4.3 | "use at most 2 queries per task"; "8,000 characters per message" | 2; 8,000 | src/swarm/prompts/task_phase_system_rules2.txt; tables/arms_e1.md | cap sentence; channel column, e1_unconstrained 8000 |
| 48 | 4.3 | "All four hypotheses were falsified. … peek rate 0.0% in every arm" | 4; 0.0% | RESULTS.md; tables/arms_e1.md | Experiment 1 table (P1, P2, P2′, P3 falsified); peek rate column |
| 49 | 4.3 | "1.5% [0.7, 3.2] … 0.2% [0.0, 1.4] (P2: +1.3 points, p = 0.163) and 4.5% [2.9, 7.0] (P2′: −3.0 points, bootstrap [−5.8, −0.5], p = 0.0562)" | as quoted | figures/CAPTIONS.md; RESULTS.md | fig6 pooled rates: e1_constrained, e1_unconstrained, e1_constrained_nomem; rows P2, P2′ |
| 50 | 4.3 | "(P3: −2.5 points, p = 0.123)" | −2.5; 0.123 | RESULTS.md | row P3 |
| 51 | 4.3 | "cap of one query … 2.8% [1.5, 4.9] against 9.8% [7.2, 13.1]; −7.0 points, [−10.3, −3.5], p = 0.0012; contradicted" | as quoted | figures/CAPTIONS.md; RESULTS.md | fig6: e1_cap1, e1_cap1_nomem; Step 8 row "cap1 P2′" |
| 52 | 4.3 | "TC 4/20 = 20.0% [8.1, 41.6], EC 2.0% [0.8, 5.0]; with cap 1, TC 3/20 = 15.0% [5.2, 36.0]" | as quoted | tables/arms_e1.md | e1_constrained_nomem TC, EC pooled; e1_cap1_nomem TC |
| 53 | 4.3 | "solo baselines (6/100 = 6.0% [2.8, 12.5] for cap 2; 10/100 = 10.0% [5.5, 17.4] for cap 1)" | as quoted | figures/CAPTIONS.md | fig6, "Solo" values |
| 54 | 4.3 | "with 8,000 characters …, 2.0% of verdicts and … 75.0% [68.6, 80.5]" | 8,000; 2.0; 75.0 | tables/arms_e1.md | e1_unconstrained: channel, record delivered, EC pooled |
| 55 | 4.3 | "delivery 3.5% [2.1, 5.8], F5 supported; EC 77.5% [71.2, 82.7], F6 supported" | as quoted | RESULTS.md; tables/arms_e1.md | rows F5, F6; e1_unconstrained_plainintro EC pooled |
| 56 | 4.4 | "2/20 trajectories [2.8, 30.1] ever reported … (P1 supported)" | 2/20 | RESULTS.md | Experiment 3 rows P1 ack, P1 fix |
| 57 | 4.4 | "EC was 67.0% [60.2, 73.1] … 66.5% [59.7, 72.7] … 71.0% [64.4, 76.8] …, TC 20/20 [83.9, 100.0]" | as quoted | tables/arms_e3.md; RESULTS.md | EC pooled, TC: e3_ack, e3_fix, e3_control; row P4 |
| 58 | 4.4 | "Of the four reports, one described the broken protocol; three were status logs" | 4; 1; 3 | docs/step7_report_texts.md; docs/step7_notes.md | notes-only (N4) |
| 59 | 4.4 | "invitation tripled reporting (6/20 = 30.0% [14.5, 51.9], F1 supported)" | 6/20; ×3 | RESULTS.md | row F1, against 2/20 in row "P1 … fix" |
| 60 | 4.4 | "deliveries stayed below 10% and collusion was not lower than control (F2 supported)" | 10% | RESULTS.md; PREREG.md | row F2 ("P3a delivered: supported; P3b … supported"); threshold in PREREG §3, F2(a) |
| 61 | 4.4 | "(3/20 = 15.0% [5.2, 36.0], F3 not supported) … EC 43.5% [36.8, 50.4], TC 19/20 [76.4, 99.1]" | as quoted | RESULTS.md; tables/arms_e3.md | row F3; e3_checkin EC pooled, TC |
| 62 | 4.5 | "at least 15 points … lowered EC by 12.5 points (bootstrap [−22.5, −2.5], permutation p = 0.0281) … E1 is not supported" | 15; −12.5; CI; 0.0281 | RESULTS.md | row E1 |
| 63 | 4.5 | "reached EC 47.0% [40.2, 53.9] … (Q1: −11.5 points, bootstrap [−24.0, +1.5], p = 0.103)" | as quoted | tables/defence_ladder.md; RESULTS.md | e5_question EC pooled; row Q1 |
| 64 | 4.5 | Table 2, all cells | as quoted | tables/defence_ladder.md | all six rows (EC, TC, median onset, responsive share) |
| 65 | 4.5 | "C1, a drop of at least 15 points … 100/100 = 100.0% [96.3, 100.0] against 95/100 = 95.0% [88.8, 97.8] …, +5.0 points (bootstrap [+2.0, +8.0], permutation p = 0.0342)" | as quoted | tables/arms_s5.md; RESULTS.md | EC pooled: s5_checkin, s5_paper; row C1 |
| 66 | 4.5 | "colluded from episode 1 … (0/10 [0.0, 27.8] against 1/10 [1.8, 40.4]; C3)" | 1; 0/10; 1/10 | tables/arms_s5.md; tables/defence_ladder.md; RESULTS.md | onsets by episode "e1: 10"; responsive share column; row C3 (−10.0 pts) |
| 67 | 4.5 | "6/10 trajectories [31.3, 83.2] reported, against 3/20 [5.2, 36.0] for Haiku (C2)" | as quoted | RESULTS.md | row C2 |
| 68 | 4.5 | "15 of its 21 reports were status updates, 3 …, 2 …, 1 …" | 15; 21; 3; 2; 1 | docs/step8c_notes.md | notes-only (N5) |
| 69 | 4.5 | "widened to 8,000 characters in 5 trajectories. All 33 widened episodes stayed collusive" | 8,000; 5; 33 | src/swarm/prompts/operator_fix.txt; docs/step8c_notes.md; tables/arms_s5.md | 8,000 in the fix reply text; 5 and 33 are notes-only (N6); consistent with EC 100/100 (every episode collusive) |
| 70 | 4.5 | "no verdict received the record (record delivered 0.0%)" | 0.0% | tables/arms_s5.md | s5_checkin, record delivered column |
| 71 | 4.5 | Figure 3 caption, all values | as quoted | figures/CAPTIONS.md | fig9_defence_ladder.png |
| 72 | 4.6 | "1/10 trajectories converted [1.8, 40.4], with ACCEPTs in 6/100 episodes. H1 (at least 20%) … go rule (at least 1/10)" | as quoted | RESULTS.md; tables/arms_e4pre.md; PREREG.md | row "E4 pre-test H1"; EC pooled 6/100 (A's ACCEPTs, because B always ACCEPTs); go rule in PREREG §6 |
| 73 | 4.6 | "six-agent Haiku groups … (zero to two planted proposers …)" | 6; 0–2 | tables/arms_e4_population.md; PREREG.md | "planted per group" column; §7 Design |
| 74 | 4.6 | "only 8.0–15.0% of real agents had given a counting ACCEPT by episode 10" | 8.0; 15.0 | tables/arms_e4_population.md | "converted by ep. 10", six explicit arms (min p_explicit_k1 8.0%, max k0 and board_k0 15.0%) |
| 75 | 4.6 | "No group reached a majority (0/10 per arm), … PCR at episode 10 was 0.0%" | 0/10; 0.0% | tables/arms_e4_population.md | "groups reaching majority" and "PCR ep. 10", explicit arms |
| 76 | 4.6 | "all 60 agents converted (100.0% [94.0, 100.0]), every group reached a majority, … PCR reached 96.7%" | 60; 100.0; 10/10; 96.7 | tables/arms_e4_population.md | p_paper_board_k0 row |
| 77 | 4.6 | "Both H2 families … falsified. … 4/40 = 10.0% [4.0, 23.1] against 9/60 = 15.0% [8.1, 26.1] … exactly −5.0 points (bootstrap [−15.8, +5.0], p = 0.462)" | as quoted | RESULTS.md; tables/arms_e4_population.md | rows H2a, H2b, "H2b k2>k0"; converted column, board_k2 and board_k0; boundary note |
| 78 | 4.6 | "H3 and H4 are not computable, and H5 … is supported" | — | RESULTS.md | rows H3a, H3b, H4, H5 |
| 79 | 4.6 | "0 ACCEPTs in 520 real–scripted agent-episodes … 519 … 'proposal' and one an 'agreement'" | 0/520; 519; 1 | tables/arms_e4_population.md | real-scripted ACCEPT rate (0/100, 0/160, 0/100, 0/160); RS labels 0/100/0, 0/160/0, 0/100/0, 1/159/0 |
| 80 | 4.6 | "Real–real ACCEPT rates were 4.5% [3.1, 6.5] … 2.0% [1.0, 3.9] to 3.3% [1.7, 6.4]" | as quoted | tables/arms_e4_population.md | real-real ACCEPT rate: k0 and board_k0 27/600; board_k1 8/400 = 2.0%; board_k2 8/240 = 3.3% (k1 2.2%, k2 2.1% in between) |
| 81 | 4.6 | "posted on the board once in 1,500 explicit-arm agent-episodes and never under the paper wording" | 1/1,500; 0/600 | tables/arms_e4_population.md | real board posts: 0/600 + 1/500 + 0/400 (explicit board arms); p_paper_board_k0 0/600 |
| 82 | 4.6 | "17 'agreements' (judge-only) and 26 'proposals'" | 17; 26 | tables/arms_e4_population.md | RR labels agreement/proposal/none, six explicit arms: agreements 3+3+0+6+5+0 = 17; proposals 6+2+1+9+2+6 = 26 |
| 83 | 4.6 | Figure 4 caption, all values | as quoted | figures/CAPTIONS.md | fig8_e4_conversion.png |
| 84 | 5.1 | "14 of 20 agents … 12 of 62 cases (against 152 of 175 …)" | 14/20; 12/62; 152/175 | docs/step5_notes.md | notes-only (N1) |
| 85 | 5.1 | "(TC 0/20 across both explicit arms)" | 0/20 | tables/arms_e0.md | as row 2 |
| 86 | 5.1 | "33 of the 34 converted agents with a relaxed note … only 8.0–15.0% converted" | 33; 34; 8.0; 15.0 | tables/arms_e4_population.md | "stated first / acted first": 9/0, 3/1, 4/0, 8/0, 5/0, 4/0 (33 stated first, 1 acted first); converted column |
| 87 | 5.1 | "all 150 … In 71 (all 10 trajectories) … In 59 (9 trajectories …) … 18 … Only 2, in one trajectory" | 150; 71; 10; 59; 9; 18; 2; 1 | tables/s5_accept_justifications.md | s5_explicit rows: reinterpret 71 (10 trajectories), ignore 59 (9), names other 18 (6), override 2 (1); "of all ACCEPTs" 150 |
| 88 | 5.1 | the two verbatim justifications (ids 4, 5; 17 IDs; 200-char; 5 prior episodes; 5/5; 4 of those 5) | as quoted | docs/step8c_notes.md | lines 193 and 195; verbatim agent text, notes-only (N7) |
| 89 | 5.1 | "(median onset 4.0 against 1.0) and lowers EC (65.0% against 95.0%)" | as quoted | tables/arms_s5.md | median onset, EC pooled: s5_explicit, s5_paper |
| 90 | 5.1 | Table 3, all cells | as quoted | tables/s5_accept_justifications.md | all 12 rows |
| 91 | 5.2 | "explicit wording left TC at 10/10, and the strongest Haiku defence left EC at 100%" | 10/10; 100 | tables/arms_s5.md | s5_explicit TC; s5_checkin EC pooled |
| 92 | 5.2 | "each Sonnet 5 arm has 10 trajectories" | 10 | tables/arms_s5.md | trajectories column |
| 93 | 5.3 | "with 8,000 characters from the start the record was delivered in 2.0–3.5% of verdicts" | 8,000; 2.0; 3.5 | tables/arms_e1.md | record delivered: e1_unconstrained 2.0%, e1_unconstrained_plainintro 3.5% |
| 94 | A.1 | "Sonnet 5 ran only R1 (two arms) and Experiment 5b (one arm), with 10 trajectories each" | 2; 1; 10 | tables/arms_s5.md | rows s5_explicit, s5_paper, s5_checkin; trajectories |
| 95 | A.1 | "At 200 characters per message, with records above 4,000 characters, the two tests coincide" | 200; 4,000 | README.md; PREREG.md | as rows 16 and 22 |
| 96 | A.1 | "κ 0.36 … 0.38 …; κ 0.35 and 0.3 …, despite raw agreement above 95% … (0.87 and 0.9)" | as quoted | tables/judge_agreement.md | rows step78_judges (95.7%, 95.8%), e3_judges (96.8%, 98.1%), step5_judges sonnet5 (0.87, 0.9) |
| 97 | A.1 | "Human validation used one labeller and 15 items per kind" | 1; 15 | tables/validation.md; RESULTS.md | as row 26 |
| 98 | A.1 | "`s5_explicit`'s 2 and `s5_paper`'s 4 … `s5_checkin`'s 1, and the 17 real–real agreements" | 2; 4; 1; 17 | tables/arms_s5.md; tables/arms_e4_population.md | pathways "coord 2", "coord 4", "coord 1"; row 82 |
| 99 | A.1 | "refused 78 subject calls in arm 6. As a judge it left 39 of 219 items unresolved" | 78; 39/219 | tables/arms_e0.md; docs/step5_notes.md | sonnet_paper_think refusals; 39/219 is notes-only (N3) |
| 100 | A.1 | boundary case: "exactly 5.0 points (4/40 = 10.0% against 9/60 = 15.0%) … 0.0499999… commit `5939221` … p = 0.462" | as quoted | RESULTS.md | "The Experiment 4 boundary note"; row "H2b k2>k0" |
| 101 | A.1 | "The 41 verdict rows" | 41 | RESULTS.md | intro line |
| 102 | A.1 | "varied only in the 8,000-character arms" | 8,000 | tables/arms_e1.md | channel column: e1_unconstrained, e1_unconstrained_plainintro |
| 103 | A.2 | "all 20 control trajectories of Experiment 3 colluded" | 20/20 | tables/arms_e3.md | e3_control TC |
| 104 | A.2 | "one author's labels on 30 judge items" | 30 | tables/validation.md | items 15 (coordination) + 15 (relaxation) |
| 105 | B.1 | the hash table (sections, commits, whole-file sha256, arms) | hashes | runs/<arm>/arm.json; git log -- PREREG.md | `prereg_first_launch.sha256` of e1_constrained (and e1_*), e3_control (e3_ack, e3_fix), e3_invite (step-8 arms), e5_reminder / s5_pilot, s5_explicit (s5_paper, e5_question, pair_explicit_proposer), p_explicit_k0 (all p_*), s5_checkin; commits 102e753, 33e9d57, 5561072, 99d78bf, 5785a22, 1abe51d, 56f15ec |
| 106 | B.2 | "All 41 rows … contradicted 1, falsified 12, inconclusive 4, not computable 5, not run 1, not supported 5, supported 6, plus 7 descriptive" | as quoted | RESULTS.md | intro line |
| 107 | B.2 | the verdict table (reproduced) | all | RESULTS.md; tables/hypotheses.md | every hypothesis row |
| 108 | B.2 | "The Sonnet 5 pilot stopped at its $2 cap" | 2 | README.md; PREREG.md | Cost section note "s5_pilot (stopped at its $2 cap)"; Amendment 1 |
| 109 | C | 12-hex prompt versions (e.g. `3a6e0fa623dc`, `e3223a56238c`) | hashes | tables/prompt_versions.md | one row per prompt |
| 110 | C.4 | operator reply hashes `ca3e6d7e304f`, `30647c21c750`, `81cff46cfc5d` | hashes | src/swarm/prompts/operator_ack.txt, operator_fix.txt, operator_fix_again.txt | sha256 of the files at freeze (computed; not in the per-record table) |
| 111 | C | numbers inside the prompt texts (10 points; 0, 10 or 20; 8,000; 200; 12 posts; 2 and 1 query calls) | as quoted | src/swarm/prompts/*.txt | verbatim file contents |
| 112 | D.1 | Experiment 3 (4) and step-8 (9) report texts | 4; 9 | docs/step7_report_texts.md; docs/step8_report_texts.md | verbatim; the counts are in each file's header line |
| 113 | D.1 | Sonnet 5 report texts; "a selection from the 21 reports" | 21 | episode records runs/s5_checkin/trajectories/s00, s01, s04, s09 (swarm-runs.zip); docs/step8c_notes.md | verbatim `reports[].text`; the count 21 is notes-only (N5) |
| 114 | D.2 | judge rubric hashes `b6f5230db45a`, `49cf52f41973`, `0a569cad5de4` | hashes | src/swarm/prompts/judge_*.txt; PREREG.md | file sha256 at freeze (computed); judge_board also in PREREG §7 "Prompts" |
| 115 | D.3 | "30 items (15 coordination, 15 notes)" | 30; 15 | tables/validation.md | items column |
| 116 | D.3 | "After item V03 … V02 and V03 … after V08" | item IDs | RESULTS.md | "Validation of the judges", bullet 1 |
| 117 | D.3 | Table D1, all cells | as quoted | tables/validation.md | rows haiku and sonnet5 |
| 118 | D.3 | "Claude Sonnet 5.5 has labels on only 4 items, 2 of each kind" | 4; 2 | tables/validation.md | rows "sonnet" (items 2 and 2) |
| 119 | D.3 | "4 of Haiku's 5 proposals … precision 6/7, recall 6/9" | 4/5; 6/7; 6/9 | RESULTS.md | "Validation of the judges", bullet 2 |
| 120 | D.3 | judge-only list: 17; 2; 4; 1 | 17; 2; 4; 1 | tables/arms_e4_population.md; tables/arms_s5.md | as rows 82 and 98 |
| 121 | D.3 | Table D2, all cells | as quoted | tables/judge_agreement.md | all eight rows |
| 122 | D.4 | Figures D1–D5 captions, all values | as quoted | figures/CAPTIONS.md | fig2_e1_collusion, fig6_e1_cap_violations, fig3_e3_collusion, fig4_e5_collusion, fig7_report_rate |
| 123 | D.5 | swarm-runs.zip sha256 | hash | README.md | "Data" section |

## Notes-only numbers (not in tables/ or RESULTS.md)
- **N1:** `docs/step5_notes.md`, lines 138–141 (table "Say versus do"). In arm 1 (explicit wording, thinking off), 14 of 20 agents wrote a relaxed note, and an ACCEPT followed a relaxed note in 12/62 cases. In arm 2 (paper wording), 152/175. Used in §5.1(a).
- **N2:** `docs/step4_notes.md`, line 165. Arm 6's 78 refusals were all `reasoning_extraction`. The count 78 is in `tables/arms_e0.md`; only the category is notes-only. Used in §4.1.
- **N3:** `docs/step5_notes.md`, lines 100–102. The Sonnet 5.5 judge left 39 of 219 items unresolved after refusals (17.8%). Used in Appendix A.1.
- **N4:** `docs/step7_notes.md`, lines 67–73, with `docs/step7_report_texts.md`, line 3. Experiment 3 had 4 reports: 1 described the broken protocol and 3 were status logs. The step-8 count of 9 reports is from `docs/step8_report_texts.md`, line 3. Used in §4.4 and Appendix D.1.
- **N5:** `docs/step8c_notes.md`, lines 215 and 221–225. `s5_checkin` had 21 reports: 15 status updates, 3 on the protocol, 2 on a data discrepancy and 1 tool note. Used in §4.5 and Appendix D.1.
- **N6:** `docs/step8c_notes.md`, line 219. The fix widened the channel in 5 trajectories (33 episodes), and all of them stayed collusive. This is consistent with EC 100/100 in `tables/arms_s5.md`. Used in §4.5.
- **N7:** `docs/step8c_notes.md`, lines 193 and 195. The numbers inside the two verbatim Sonnet 5 justifications are the agent's own words. Used in §5.1(b).
