"""
Automatic evaluation harness used for Chapter 5 (Experiments & Results).

Because running a full human evaluation study (as ConvAI2 / the original
PersonaChat paper did) is outside the scope of what can be executed inside
this prototype, this script implements the two automatic proxies that are
most commonly reported alongside human judgements in the persona-dialogue
literature:

* **Persona-consistency proxy (C-proxy)** - TF-IDF cosine similarity
  between each generated reply and the gold persona sentences for that
  conversation. This is a coarser stand-in for the NLI-based "C score"
  used in the original PersonaChat / ConvAI2 evaluation (Dinan et al.,
  2020), chosen here because it needs no pretrained entailment model and
  runs fully offline.
* **Distinct-1 / Distinct-2** - the standard lexical-diversity metric from
  Li et al. (2016), measuring the ratio of unique unigrams/bigrams to
  total unigrams/bigrams across all generated replies. Low Distinct
  scores are the classic symptom of generic, "I don't know"-style
  responses that persona conditioning is meant to fix.

Run:
    python evaluate.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))  # chatbot_engine.py/profiler.py live there now
from chatbot_engine import ChatEngine
from profiler import ImplicitProfiler


def distinct_n(sentences: List[str], n: int) -> float:
    ngrams = set()
    total = 0
    for s in sentences:
        tokens = s.lower().split()
        grams = list(zip(*[tokens[i:] for i in range(n)]))
        ngrams.update(grams)
        total += len(grams)
    return len(ngrams) / total if total else 0.0


def persona_consistency_proxy(reply: str, persona_sentences: List[str]) -> float:
    corpus = persona_sentences + [reply]
    vec = TfidfVectorizer().fit_transform(corpus)
    sims = cosine_similarity(vec[-1], vec[:-1])
    return float(sims.max())


def main():
    data_path = Path(__file__).parent / "data" / "personachat_sample.json"
    dialogues = json.loads(data_path.read_text())

    profiler = ImplicitProfiler(storage_dir="eval_profiles")
    engine = ChatEngine()
    print(f"Response back-end under test: {engine.backend_name}\n")

    all_replies: List[str] = []
    consistency_scores: List[float] = []

    for i, dialogue in enumerate(dialogues):
        user_id = f"eval_user_{i}"
        history: List[dict] = []
        persona = dialogue["persona"]
        turns = [t["text"] for t in dialogue["conversation"] if t["speaker"] == "B"]
        user_turns = [t["text"] for t in dialogue["conversation"] if t["speaker"] == "A"]

        for user_msg in user_turns:
            profile = profiler.update(user_id, user_msg)
            reply = engine.reply(user_msg, profile, history)
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": reply})

            score = persona_consistency_proxy(reply, persona)
            all_replies.append(reply)
            consistency_scores.append(score)

            print(f"[dialogue {i}] user: {user_msg}")
            print(f"[dialogue {i}] bot : {reply}")
            print(f"[dialogue {i}] persona-consistency proxy: {score:.3f}\n")

    avg_c = sum(consistency_scores) / len(consistency_scores)
    d1 = distinct_n(all_replies, 1)
    d2 = distinct_n(all_replies, 2)

    print("=" * 60)
    print(f"Average persona-consistency proxy : {avg_c:.3f}")
    print(f"Distinct-1                        : {d1:.3f}")
    print(f"Distinct-2                        : {d2:.3f}")
    print(f"Total replies evaluated           : {len(all_replies)}")


if __name__ == "__main__":
    main()
