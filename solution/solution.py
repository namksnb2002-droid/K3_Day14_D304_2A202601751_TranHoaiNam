"""
Day 14 — AI Evaluation & Benchmarking Pipeline
AICB-P1: AI Practical Competency Program, Phase 1

Key concepts from lecture:
    - Evaluation = Scientific Method for AI (Hypothesis → Experiment → Measure → Conclude → Iterate)
    - 4 nhóm metrics: Task Completion, Answer Quality, RAG-Specific, Business
    - RAG pipeline metrics: Context Recall → Context Precision → Faithfulness → Answer Relevancy
    - LLM-as-Judge: rubric scoring 1-5, detect bias (positional, verbosity, self-preference)
    - Golden dataset: stratified sampling (5 Easy + 7 Medium + 5 Hard + 3 Adversarial)
    - Failure taxonomy: hallucination, irrelevant, incomplete, off_topic, refusal
    - 5 Whys method for root cause analysis
    - CI/CD integration: eval as quality gate (score < threshold = block deploy)
    - Continuous Improvement Loop: Evaluate → Analyze → Improve → Augment → Repeat

Instructions:
    1. Fill in every required section marked with TODO.
    2. Do NOT change class/function signatures. The optional ``contexts``
       parameter in ``run_full_eval`` is part of the required interface.
    3. Copy this file to solution/solution.py when done.
    4. Run: pytest tests/ -v

The reranking helper is an optional bonus exercise and may remain unimplemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Task 1 — Data Models (Golden Dataset + Evaluation Results)
# ---------------------------------------------------------------------------

@dataclass
class QAPair:
    """
    A question-answer pair for evaluation (part of the Golden Dataset).

    Fields:
        question:        The question to answer.
        expected_answer: The reference/ground-truth answer (expert-written).
        context:         Source context (may be empty string if not applicable).
        metadata:        Optional metadata dict (difficulty, category, etc.).
        retrieved_contexts: List of retrieved chunks (ORDER = retriever rank).
    """
    question: str
    expected_answer: str
    context: str = ""
    metadata: dict = field(default_factory=dict)
    retrieved_contexts: list = field(default_factory=list)


@dataclass
class EvalResult:
    """
    Evaluation result for a single Q&A pair.

    Fields:
        qa_pair:           The original QAPair.
        actual_answer:     What the agent actually returned.
        faithfulness:      Float 0-1, how grounded the answer is in context.
        relevance:         Float 0-1, how relevant the answer is to the question.
        completeness:      Float 0-1, how complete the answer is vs expected.
        passed:            True if all three scores >= 0.5.
        failure_type:      None if passed, otherwise one of:
                           "hallucination", "irrelevant", "incomplete", "off_topic".
        context_precision: Float 0-1 or None — quality of retrieval ranking.
        context_recall:    Float 0-1 or None — coverage of expected by context.
    """
    qa_pair: QAPair
    actual_answer: str
    faithfulness: float
    relevance: float
    completeness: float
    passed: bool
    failure_type: str | None = None
    context_precision: float | None = None
    context_recall: float | None = None

    def overall_score(self) -> float:
        """Compute the average of faithfulness, relevance, and completeness."""
        return (self.faithfulness + self.relevance + self.completeness) / 3.0


# ---------------------------------------------------------------------------
# Task 2 — RAGAS Evaluator (Simplified word-overlap heuristic)
# ---------------------------------------------------------------------------

STOPWORDS: set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "of", "in", "on", "at", "to", "for", "with", "as", "by", "and", "or",
    "it", "its", "this", "that", "these", "those", "from", "into", "than",
}


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokenization, ignoring punctuation and stopwords."""
    if not text:
        return set()
    tokens = re.findall(r"\b\w+\b", text.lower())
    return {t for t in tokens if t not in STOPWORDS}


class RAGASEvaluator:
    """
    Evaluates RAG pipeline outputs using RAGAS-inspired heuristics.
    """

    def evaluate_faithfulness(self, answer: str, context: str) -> float:
        """
        Measure how grounded the answer is in the context.
        """
        answer_tokens = _tokenize(answer)
        if not answer_tokens:
            return 1.0
        context_tokens = _tokenize(context)
        score = len(answer_tokens & context_tokens) / len(answer_tokens)
        return min(max(float(score), 0.0), 1.0)

    def evaluate_relevance(self, answer: str, question: str) -> float:
        """
        Measure how relevant the answer is to the question.
        """
        question_tokens = _tokenize(question)
        if not question_tokens:
            return 1.0
        answer_tokens = _tokenize(answer)
        score = len(answer_tokens & question_tokens) / len(question_tokens)
        return min(max(float(score), 0.0), 1.0)

    def evaluate_completeness(self, answer: str, expected: str) -> float:
        """
        Measure how well the answer covers the expected answer.
        """
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        answer_tokens = _tokenize(answer)
        score = len(answer_tokens & expected_tokens) / len(expected_tokens)
        return min(max(float(score), 0.0), 1.0)

    def evaluate_context_recall(self, contexts: list[str], expected: str) -> float:
        """Context Recall — coverage of expected answer by UNION of retrieved chunks."""
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        union_tokens: set[str] = set()
        for chunk in contexts:
            union_tokens.update(_tokenize(chunk))
        score = len(expected_tokens & union_tokens) / len(expected_tokens)
        return min(max(float(score), 0.0), 1.0)

    def evaluate_context_precision(
        self,
        contexts: list[str],
        expected: str,
        relevance_threshold: float = 0.1,
    ) -> float:
        """Context Precision — RANK-AWARE Average Precision (AP@K)."""
        expected_tokens = _tokenize(expected)
        if not expected_tokens:
            return 1.0
        if not contexts:
            return 0.0

        rel_flags: list[bool] = []
        for chunk in contexts:
            chunk_tokens = _tokenize(chunk)
            rel = len(chunk_tokens & expected_tokens) / len(expected_tokens)
            rel_flags.append(rel >= relevance_threshold)

        num_relevant = sum(1 for is_rel in rel_flags if is_rel)
        if num_relevant == 0:
            return 0.0

        ap_sum = 0.0
        rel_count_so_far = 0
        for k, is_rel in enumerate(rel_flags, start=1):
            if is_rel:
                rel_count_so_far += 1
                precision_at_k = rel_count_so_far / k
                ap_sum += precision_at_k

        return min(max(float(ap_sum / num_relevant), 0.0), 1.0)

    def run_full_eval(
        self,
        answer: str,
        question: str,
        context: str,
        expected: str,
        contexts: list[str] | None = None,
    ) -> EvalResult:
        """
        Run answer-side and optional retrieval-side evaluations.
        """
        faithfulness = self.evaluate_faithfulness(answer, context)
        relevance = self.evaluate_relevance(answer, question)
        completeness = self.evaluate_completeness(answer, expected)

        passed = (faithfulness >= 0.5 and relevance >= 0.5 and completeness >= 0.5)

        failure_type: str | None = None
        if not passed:
            if faithfulness < 0.3:
                failure_type = "hallucination"
            elif relevance < 0.3:
                failure_type = "irrelevant"
            elif completeness < 0.3:
                failure_type = "incomplete"
            else:
                failure_type = "off_topic"

        context_recall: float | None = None
        context_precision: float | None = None
        if contexts is not None:
            context_recall = self.evaluate_context_recall(contexts, expected)
            context_precision = self.evaluate_context_precision(contexts, expected)

        qa_pair = QAPair(
            question=question,
            expected_answer=expected,
            context=context,
            retrieved_contexts=contexts if contexts is not None else [],
        )

        return EvalResult(
            qa_pair=qa_pair,
            actual_answer=answer,
            faithfulness=faithfulness,
            relevance=relevance,
            completeness=completeness,
            passed=passed,
            failure_type=failure_type,
            context_precision=context_precision,
            context_recall=context_recall,
        )


# ---------------------------------------------------------------------------
# Reranking helper (used by Exercise 3.5 — boosting Context Precision)
# ---------------------------------------------------------------------------

def rerank_by_overlap(contexts: list[str], query: str) -> list[str]:
    """A minimal lexical reranker: sort chunks by word overlap with query."""
    query_tokens = _tokenize(query)
    return sorted(contexts, key=lambda c: len(_tokenize(c) & query_tokens), reverse=True)


# ---------------------------------------------------------------------------
# Task 3 — LLM Judge
# ---------------------------------------------------------------------------

class LLMJudge:
    """
    Uses an LLM to score AI responses according to a rubric.
    """

    def __init__(self, judge_llm_fn: Callable[[str], str]) -> None:
        self.judge_llm_fn = judge_llm_fn

    def score_response(
        self,
        question: str,
        answer: str,
        rubric: dict[str, Any],
    ) -> dict[str, Any]:
        rubric_str = "\n".join(f"- {k}: {v}" for k, v in rubric.items())
        prompt = (
            f"Question: {question}\nAnswer: {answer}\nRubric:\n{rubric_str}\n"
            "Please score each criterion between 0.0 and 1.0 as JSON."
        )
        raw_response = self.judge_llm_fn(prompt)
        try:
            import json
            json_text = raw_response
            if "```json" in json_text:
                json_text = json_text.split("```json")[1].split("```")[0].strip()
            elif "```" in json_text:
                json_text = json_text.split("```")[1].split("```")[0].strip()
            parsed = json.loads(json_text)
            if isinstance(parsed, dict):
                scores = {}
                for k in rubric.keys():
                    if k in parsed and isinstance(parsed[k], (int, float)):
                        scores[k] = float(parsed[k])
                    else:
                        scores[k] = 0.5
                return {"scores": scores, "reasoning": raw_response}
        except Exception:
            pass

        default_scores = {k: 0.5 for k in rubric.keys()}
        return {"scores": default_scores, "reasoning": raw_response}

    def detect_bias(self, scores_batch: list[dict[str, Any]]) -> dict[str, Any]:
        if not scores_batch:
            return {"positional_bias": False, "leniency_bias": False, "severity_bias": False}

        all_scores: list[float] = []
        for item in scores_batch:
            scores_dict = item.get("scores", {})
            for val in scores_dict.values():
                if isinstance(val, (int, float)):
                    all_scores.append(float(val))

        avg_score = sum(all_scores) / len(all_scores) if all_scores else 0.5
        leniency_bias = avg_score > 0.8
        severity_bias = avg_score < 0.3

        first_score_avg = 0.0
        rest_score_avg = 0.0
        if len(scores_batch) > 1:
            first_vals = list(scores_batch[0].get("scores", {}).values())
            rest_vals: list[float] = []
            for item in scores_batch[1:]:
                rest_vals.extend(item.get("scores", {}).values())
            if first_vals and rest_vals:
                first_score_avg = float(sum(first_vals)) / len(first_vals)
                rest_score_avg = float(sum(rest_vals)) / len(rest_vals)

        positional_bias = len(scores_batch) > 1 and (first_score_avg - rest_score_avg > 0.2)

        return {
            "positional_bias": positional_bias,
            "leniency_bias": leniency_bias,
            "severity_bias": severity_bias,
        }


# ---------------------------------------------------------------------------
# Task 4 — Benchmark Runner
# ---------------------------------------------------------------------------

class BenchmarkRunner:
    """
    Runs a full evaluation benchmark.
    """

    def run(
        self,
        qa_pairs: list[QAPair],
        agent_fn: Callable[[str], str],
        evaluator: RAGASEvaluator,
    ) -> list[EvalResult]:
        results: list[EvalResult] = []
        for pair in qa_pairs:
            answer = agent_fn(pair.question)
            res = evaluator.run_full_eval(
                answer=answer,
                question=pair.question,
                context=pair.context,
                expected=pair.expected_answer,
                contexts=pair.retrieved_contexts if pair.retrieved_contexts else None,
            )
            res.qa_pair = pair
            results.append(res)
        return results

    def generate_report(self, results: list[EvalResult]) -> dict[str, Any]:
        if not results:
            return {
                "total": 0,
                "passed": 0,
                "pass_rate": 0.0,
                "avg_faithfulness": 0.0,
                "avg_relevance": 0.0,
                "avg_completeness": 0.0,
                "avg_context_recall": None,
                "avg_context_precision": None,
                "failure_types": {},
            }

        total = len(results)
        passed = sum(1 for r in results if r.passed)
        pass_rate = float(passed / total)

        avg_faithfulness = float(sum(r.faithfulness for r in results) / total)
        avg_relevance = float(sum(r.relevance for r in results) / total)
        avg_completeness = float(sum(r.completeness for r in results) / total)

        recalls = [r.context_recall for r in results if r.context_recall is not None]
        precisions = [r.context_precision for r in results if r.context_precision is not None]

        avg_context_recall = float(sum(recalls) / len(recalls)) if recalls else None
        avg_context_precision = float(sum(precisions) / len(precisions)) if precisions else None

        failure_types: dict[str, int] = {}
        for r in results:
            if not r.passed and r.failure_type:
                failure_types[r.failure_type] = failure_types.get(r.failure_type, 0) + 1

        return {
            "total": total,
            "passed": passed,
            "pass_rate": pass_rate,
            "avg_faithfulness": avg_faithfulness,
            "avg_relevance": avg_relevance,
            "avg_completeness": avg_completeness,
            "avg_context_recall": avg_context_recall,
            "avg_context_precision": avg_context_precision,
            "failure_types": failure_types,
        }

    def run_regression(self, new_results: list, baseline_results: list) -> dict:
        new_f = float(sum(r.faithfulness for r in new_results) / len(new_results)) if new_results else 0.0
        new_r = float(sum(r.relevance for r in new_results) / len(new_results)) if new_results else 0.0
        new_c = float(sum(r.completeness for r in new_results) / len(new_results)) if new_results else 0.0

        base_f = float(sum(r.faithfulness for r in baseline_results) / len(baseline_results)) if baseline_results else 0.0
        base_r = float(sum(r.relevance for r in baseline_results) / len(baseline_results)) if baseline_results else 0.0
        base_c = float(sum(r.completeness for r in baseline_results) / len(baseline_results)) if baseline_results else 0.0

        regressions: list[str] = []
        if base_f - new_f > 0.05:
            regressions.append("faithfulness")
        if base_r - new_r > 0.05:
            regressions.append("relevance")
        if base_c - new_c > 0.05:
            regressions.append("completeness")

        return {
            "new_avg_faithfulness": new_f,
            "new_avg_relevance": new_r,
            "new_avg_completeness": new_c,
            "baseline_avg_faithfulness": base_f,
            "baseline_avg_relevance": base_r,
            "baseline_avg_completeness": base_c,
            "regressions": regressions,
            "passed": len(regressions) == 0,
        }

    def identify_failures(
        self,
        results: list[EvalResult],
        threshold: float = 0.5,
    ) -> list[EvalResult]:
        failures: list[EvalResult] = []
        for r in results:
            if (
                r.faithfulness < threshold
                or r.relevance < threshold
                or r.completeness < threshold
            ):
                failures.append(r)
        return failures


# ---------------------------------------------------------------------------
# Task 5 — Failure Analyzer
# ---------------------------------------------------------------------------

class FailureAnalyzer:
    """
    Analyzes failed evaluation results to identify patterns and suggest fixes.
    """

    def categorize_failures(
        self, failures: list[EvalResult]
    ) -> dict[str, int]:
        categories: dict[str, int] = {}
        for f in failures:
            ftype = f.failure_type if f.failure_type else "unknown"
            categories[ftype] = categories.get(ftype, 0) + 1
        return categories

    def find_root_cause(self, failure: EvalResult) -> str:
        scores = {
            "faithfulness": failure.faithfulness,
            "relevance": failure.relevance,
            "completeness": failure.completeness,
        }
        min_score = min(scores.values())
        min_keys = [k for k, v in scores.items() if v == min_score]

        if len(min_keys) > 1:
            return "Multiple issues detected — review full pipeline"

        lowest = min_keys[0]
        if lowest == "faithfulness":
            return "Context is missing or irrelevant — improve retrieval"
        elif lowest == "relevance":
            return "Answer does not address the question — improve prompt clarity"
        elif lowest == "completeness":
            return "Answer is missing key information — increase context window or improve generation"
        else:
            return "Multiple issues detected — review full pipeline"

    def generate_improvement_log(self, failures: list, suggestions: list[str]) -> str:
        lines = [
            "| Failure ID | Type | Root Cause | Suggested Fix | Status |",
            "|------------|------|------------|---------------|--------|",
        ]
        for idx, f in enumerate(failures, 1):
            fid = f"F{idx:03d}"
            ftype = f.failure_type or "Unknown"
            cause = self.find_root_cause(f)
            sugg = suggestions[idx - 1] if idx - 1 < len(suggestions) else "Investigate issue"
            lines.append(f"| {fid} | {ftype} | {cause} | {sugg} | Open |")
        return "\n".join(lines)

    def generate_improvement_suggestions(
        self, failures: list[EvalResult]
    ) -> list[str]:
        if not failures:
            return []

        cats = self.categorize_failures(failures)
        suggestions: list[str] = []
        if cats.get("hallucination", 0) > 0:
            suggestions.append("Implement hallucination checker to filter unsupported claims")
        if cats.get("incomplete", 0) > 0:
            suggestions.append("Add few-shot examples showing complete answers to improve completeness")
        if cats.get("irrelevant", 0) > 0 or cats.get("off_topic", 0) > 0:
            suggestions.append("Improve prompt clarity and intent routing to maintain relevance")

        default_suggestions = [
            "Increase chunk size in RAG pipeline to reduce context fragmentation",
            "Add few-shot examples showing complete answers to improve completeness",
            "Implement hallucination checker to filter unsupported claims",
        ]
        for ds in default_suggestions:
            if ds not in suggestions:
                suggestions.append(ds)

        return suggestions


if __name__ == "__main__":
    qa_pairs = [
        QAPair(
            question="What is RAG?",
            expected_answer="RAG stands for Retrieval-Augmented Generation, which combines retrieval with text generation.",
            context="RAG is a technique that retrieves relevant documents and uses them to ground LLM generation.",
            metadata={"difficulty": "easy", "category": "definition"},
        ),
    ]

    evaluator = RAGASEvaluator()
    runner = BenchmarkRunner()

    def mock_agent(question: str) -> str:
        return f"Based on my knowledge: {question[:30]}... The answer involves key concepts."

    results = runner.run(qa_pairs, mock_agent, evaluator)
    report = runner.generate_report(results)
    print("=== Benchmark Report ===")
    for k, v in report.items():
        print(f"  {k}: {v}")
