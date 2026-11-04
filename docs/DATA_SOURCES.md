# Data sources

What is ours, what is not, and what is committed.

## Written by the project author (committed)
- `data/corpus.txt`: the film knowledge base (KGF, Salaar, Dragon, Nolan films, "universe notes"), written in the author's own words.
- `evals/golden.yaml`: 58 questions; `evals/judge_labels.yaml`: 38 labelled answers. **Author-written, not independent.**

## Public datasets (NOT committed; fetched on demand with `make benchmarks`)
Fetched through Hugging Face's public rows API into `data/benchmarks/` (gitignored); `MANIFEST.json` there records the dataset, split, rows read and the licence text found on the dataset card at fetch time.

| Use | Dataset (Hugging Face id) | Human-made part | Licence on the HF card |
|---|---|---|---|
| Second knowledge base + 60-question golden set (answer vs refuse) | `rajpurkar/squad_v2`, validation split | Questions, answers, and the *unanswerable* questions were written by crowd-workers | CC BY-SA 4.0 |
| Grader accuracy | `wandb/RAGTruth-processed`, test split, QA task only | Hallucination spans marked by human annotators | **Not stated on the card** |
| Grader accuracy | `pietrolesci/nli_fever`, dev split | Claims labelled SUPPORTS / REFUTES / NOT ENOUGH INFO by human annotators | **Not stated on the card** |

Why nothing is committed: two of the three cards state no redistribution licence, so the repo downloads rather than redistributes. If you share outputs derived from these datasets, check the original licences (SQuAD 2.0 is CC BY-SA 4.0, which requires attribution and share-alike).

Original works (cited from memory; please verify before citing formally): SQuAD 2.0, Rajpurkar, Jia and Liang, 2018; RAGTruth, Niu et al., 2024; FEVER, Thorne et al., 2018.

## Conversion choices worth knowing
- **SQuAD 2.0:** the knowledge base is every paragraph fetched (131 paragraphs from 16 articles) so the retriever has distractors; the 60 questions come from paragraphs that have both an answerable and an unanswerable question. An answerable question passes when the answer contains one of the human-provided answer strings.
- **Known SQuAD noise:** an "unanswerable" question is unanswerable *given its own paragraph*. Some are negation tricks ("who was *not* the ideologue of...") that an agent can answer truthfully from another passage, and the keyword check ignores nuance such as a dropped "most". In a 4-question smoke run, both failures were of this kind (one was the grader correctly noting that "resonated with" does not support "resonated most with"). Expect a ceiling below 100% that is not the agent's fault.
- **RAGTruth:** only QA rows (short), under 2,500 characters of context and 700 of answer, 20 hallucinated + 20 clean. "Hallucinated" means the annotators marked at least one span.
- **FEVER:** in this Hugging Face copy the claim is in the `premise` column and the evidence in `hypothesis` (the reverse of the usual meaning), which was verified by reading rows. 20 supported vs 20 not supported (refuted or not enough info; both mean "the evidence does not support the claim").
- Samples are seeded (`seed=0`), so the same rows are chosen every time, but the upstream datasets could change; `MANIFEST.json` records what was read.
