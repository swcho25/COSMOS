"""Offline financial polarity of a target's grounded, cleaned news contexts.

FinBERT is a sentence classifier, NOT an aspect-trained company model, GNN, or
stock-return predictor. Attribution here is a conservative lexical guard over
upstream source-grounded evidence, not proof of entity identity. A context must
contain a supplied target name and no supplied other-company name. Ambiguous
attribution, conflicting positive/negative contexts, missing evidence/language,
and overlength inputs produce NULL labels, never a fabricated neutral label.

Input rows contain article_id, ticker, company_name, original title/body,
cleaned_title/cleaned_body, evidence, target_name_aliases, other_company_names,
and optional language/input_sha256. Evidence uses field/start/end/text (source
and quote aliases are accepted) plus cleaned_quote. Offsets address ORIGINAL
text; the actual input must also occur in the cleaned corresponding field.
With no cleaned fields, the existing audited article sanitizer is applied.
For evidence with cleaned_start/end, the entire article's existing cleaning
and offset map are replayed. Both complete cleaned fields, the exact cleaned
slice, and the enclosing original endpoints must match. A title is never
reinterpreted as a standalone body paragraph by this mapped validation path.
Legacy evidence without cleaned coordinates retains the excerpt-cleanup check.

Each eligible sentence is classified separately without a synthetic target
prefix or token truncation. Distinct spans have equal weight. Positive AND
negative sentence argmaxes cause a NULL aggregate; all per-context outputs
remain available. Model probabilities are uncalibrated financial-polarity
scores. Models, tokenizer files, source spans, actual inputs, and code hashes
are recorded. This module neither promotes provisional selection nor writes
service/HDFS/DB data. All transformer loads are local-only.
"""
from __future__ import annotations

import gc
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re

from lake_sentiment import LABELS, LANGUAGE_ROLES, explicit_language, label_indexes, load_model_specs
from news_content import remove_live_quotes, sanitize_article

VERSION = "kg-target-context-finbert-v1.1"
LABEL_SCOPE = "target_grounded_context_financial_polarity"
MAX_TOKENS = 512
HERE = Path(__file__).resolve().parent
_FUNCTION_WORDS = frozenset("a an the and or but of to in for on with from by as is are was were be been has have had its that this it at will said after before while their than into about which not".split())


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def detect_language(row, text):
    """Explicit language or conservative script/function-word heuristic; no region."""
    supplied = row.get("language")
    if supplied is not None:
        return explicit_language(supplied), {"method": "explicit_language", "supplied": supplied,
                                           "source": row.get("language_source", "caller")}
    hangul = len(re.findall(r"[가-힣]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    letters = sum(char.isalpha() for char in text)
    words = re.findall(r"[A-Za-z]+", text.lower())
    function_words = sorted(set(words) & _FUNCTION_WORDS)
    audit = {"method": "script_function_words_heuristic_v1", "hangul_letters": hangul,
             "latin_letters": latin, "all_letters": letters,
             "latin_words": len(words), "distinct_function_words": function_words}
    if hangul >= 3 and hangul / max(1, hangul + latin) >= 0.15 and (hangul + latin) / max(1, letters) >= 0.9:
        return "ko", audit
    if hangul == 0 and latin / max(1, letters) >= 0.9 and len(words) >= 5 and len(function_words) >= 2:
        return "en", audit
    return None, audit


def _names(values, field):
    if not isinstance(values, (list, tuple)) or any(not isinstance(x, str) or not x.strip() for x in values):
        raise ValueError(f"{field} must be a list of nonempty names")
    return list(dict.fromkeys(x.strip() for x in values))


@lru_cache(maxsize=8192)
def _name_pattern(name):
    # Whitespace variation and case only; English boundaries prevent 'Appleton'.
    parts = re.split(r"\s+", name.strip())
    pattern = r"\s+".join(re.escape(x) for x in parts)
    if re.match(r"[A-Za-z0-9]", name):
        pattern = r"(?<![A-Za-z0-9])" + pattern
    if re.search(r"[A-Za-z0-9]$", name):
        pattern += r"(?![A-Za-z0-9])"
    return re.compile(pattern, flags=re.IGNORECASE)


def _surface(name, text):
    return _name_pattern(name).search(text) is not None


def _name_key(name):
    return re.sub(r"\s+", "", name).casefold()


def _prepare(row):
    for key in ("article_id", "ticker", "company_name"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError(f"Missing/invalid {key}")
    original = {key: row.get(key, "") for key in ("title", "body")}
    if any(not isinstance(v, str) for v in original.values()):
        raise ValueError("title/body must be strings")
    cleaned_default = sanitize_article(original)
    cleaned = {key: row.get("cleaned_" + key, cleaned_default[key]) for key in original}
    if any(not isinstance(v, str) for v in cleaned.values()):
        raise ValueError("cleaned_title/cleaned_body must be strings")
    aliases = _names(row.get("target_name_aliases", []), "target_name_aliases")
    target_names = list(dict.fromkeys([row["company_name"], *aliases]))
    other_names = _names(row.get("other_company_names", []), "other_company_names")
    target_keys = {_name_key(x) for x in target_names}
    other_names = [x for x in other_names if _name_key(x) not in target_keys]
    language, language_audit = detect_language(row, cleaned["title"] + "\n" + cleaned["body"])
    result = {"article_id": row["article_id"], "ticker": row["ticker"],
              "company_name": row["company_name"], "input_sha256": row.get("input_sha256"),
              "source_content_sha256": _json_sha(original), "cleaned_content_sha256": _json_sha(cleaned),
              "version": VERSION, "label_scope": LABEL_SCOPE, "status": "pending",
              "label": None, "score": None, "probabilities": None, "model_max_probability": None,
              "probability_calibrated": False, "language": language, "language_audit": language_audit,
              "attribution_method": "upstream_evidence_plus_lexical_name_guard_v1",
              "target_name_aliases": target_names, "other_company_names_count": len(other_names),
              "attribution_names_sha256": _json_sha({"target": target_names, "other": other_names}),
              "attribution_scope": "supplied_name_vocabulary_only_not_all_entities",
              "contexts": [], "aggregation": "equal_weight_unique_contexts_positive_negative_conflict_null",
              "validated_auto_selection": False, "production_approved": False,
              "is_stock_price_prediction": False}
    evidence = row.get("evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("evidence must be a list")
    seen, issues = set(), []
    source_maps = None
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("evidence item must be an object")
        field = item.get("field", item.get("source"))
        start, end = item.get("start"), item.get("end")
        quote = item.get("text", item.get("quote"))
        if (field not in original or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(original[field]) or not isinstance(quote, str)
                or original[field][start:end] != quote):
            raise ValueError("Evidence does not match original field/start/end/text")
        key = (field, start, end)
        if key in seen:
            continue
        seen.add(key)
        actual = item.get("cleaned_quote")
        if actual is None:
            actual = sanitize_article({"title": "", "body": quote})["body"].strip()
        if not isinstance(actual, str):
            raise ValueError("cleaned_quote must be text")
        if actual and actual not in cleaned[field]:
            raise ValueError("cleaned_quote does not occur in corresponding cleaned source")
        if "cleaned_start" in item or "cleaned_end" in item:
            cleaned_start, cleaned_end = item.get("cleaned_start"), item.get("cleaned_end")
            if (type(cleaned_start) is not int or type(cleaned_end) is not int
                    or not 0 <= cleaned_start < cleaned_end <= len(cleaned[field])):
                raise ValueError("Invalid cleaned source offsets")
            if source_maps is None:
                from kg_grounded_news import clean_article
                replayed, source_maps, _audit = clean_article(original)
                if any(replayed[key] != cleaned[key] for key in original):
                    raise ValueError("Complete cleaned source differs from full-article replay")
            if cleaned[field][cleaned_start:cleaned_end] != actual:
                raise ValueError("cleaned_quote differs from exact cleaned source offsets")
            if (source_maps[field][cleaned_start] != start
                    or source_maps[field][cleaned_end - 1] + 1 != end):
                raise ValueError("Cleaned offsets do not map to the original evidence endpoints")
            source_validation = {"method": "full_article_cleaned_slice_and_original_offset_map_v1",
                                 "cleaned_start": cleaned_start, "cleaned_end": cleaned_end}
        else:
            cleaned_original_quote = sanitize_article({"title": "", "body": quote})["body"]
            if actual and " ".join(actual.split()) not in " ".join(cleaned_original_quote.split()):
                raise ValueError("cleaned_quote is not a cleaned span of this original quote")
            source_validation = {"method": "legacy_excerpt_cleanup_substring_v1"}
        if remove_live_quotes(actual) != actual:
            raise ValueError("cleaned_quote still contains a live stock widget")
        context = {"field": field, "start": start, "end": end, "quote": quote,
                   "actual_input": actual, "input_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest(),
                   "source_validation": source_validation,
                   "matched_target_names": [name for name in target_names if _surface(name, actual)],
                   "matched_other_company_names": [name for name in other_names if _surface(name, actual)],
                   "status": "pending", "tokens": None, "truncated": False,
                   "probabilities": None, "label": None}
        attribution = item.get("attribution")
        tickers = item.get("target_tickers")
        if not actual.strip():
            context["status"] = "empty_context"
        elif (attribution is not None and attribution != "target_only"
              or tickers is not None and (not isinstance(tickers, list) or set(tickers) != {row["ticker"]})
              or not context["matched_target_names"] or context["matched_other_company_names"]):
            context["status"] = "ambiguous_target_context"
        if context["status"] != "pending":
            issues.append(context["status"])
        result["contexts"].append(context)
    if not evidence:
        result["status"] = "missing_evidence"
    elif issues:
        result["status"] = "ambiguous_target_context" if "ambiguous_target_context" in issues else issues[0]
    elif language is None:
        result["status"] = "unsupported_language"
    return result


def _probabilities(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError("Expected three probabilities in negative/neutral/positive order")
    p = [float(x) for x in values]
    if any(not math.isfinite(x) or not 0 <= x <= 1 for x in p) or abs(sum(p) - 1) > 1e-5:
        raise ValueError("Invalid sentiment probabilities")
    return dict(zip(LABELS, p))


def _label(probabilities):
    maximum = max(probabilities.values())
    winners = [label for label, p in probabilities.items() if p == maximum]
    return winners[0].upper() if len(winners) == 1 else None


class _HFClassifier:
    def __init__(self, spec, device):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested but unavailable")
        self.torch, self.device = torch, device
        torch.set_num_threads(min(4, max(1, os.cpu_count() or 1)))
        self.tokenizer = AutoTokenizer.from_pretrained(spec["path"], local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            spec["path"], local_files_only=True, trust_remote_code=False).to(device).eval()
        labels = label_indexes(self.model.config.to_dict())
        if labels != spec["label_indexes"]:
            raise ValueError("Loaded model label mapping differs from frozen config")
        self.order = [labels[label] for label in LABELS]
        self.max_tokens = min(MAX_TOKENS, self.model.config.max_position_embeddings,
                              int(self.tokenizer.model_max_length))

    def token_lengths(self, texts):
        return [len(ids) for ids in self.tokenizer(texts, add_special_tokens=True, truncation=False,
                                                  padding=False)["input_ids"]]

    def predict(self, texts):
        inputs = self.tokenizer(texts, add_special_tokens=True, truncation=False,
                                padding=True, return_tensors="pt").to(self.device)
        with self.torch.inference_mode():
            logits = self.model(**inputs).logits
            if logits.ndim != 2 or logits.shape[1] != 3:
                raise ValueError("Unexpected model output shape")
            return logits.softmax(-1)[:, self.order].cpu().tolist()


class TargetSentimentPredictor:
    """Reusable, lazy local model cache. predict accepts bounded batches of rows."""
    def __init__(self, bundle, device="cuda", batch_size=32, *, runtime_factory=None):
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.device, self.batch_size = device, batch_size
        self.specs, pretrained_sha = load_model_specs(Path(bundle))
        self._factory, self._runtimes = runtime_factory or _HFClassifier, {}
        models = {}
        for role, spec in self.specs.items():
            path = Path(spec["path"])
            files = {file.relative_to(path).as_posix(): _sha(file) for file in sorted(path.rglob("*")) if file.is_file()}
            models[role] = {"repo": spec["repo"], "revision": spec["revision"],
                            "files_sha256": files, "files_manifest_sha256": _json_sha(files),
                            "label_indexes": spec["label_indexes"]}
        self.provenance = {"version": VERSION, "pretrained_sha256": pretrained_sha, "models": models,
                           "code_sha256": {"kg_target_sentiment.py": _sha(Path(__file__)),
                                           "lake_sentiment.py": _sha(HERE / "lake_sentiment.py"),
                                           "kg_grounded_news.py": _sha(HERE / "kg_grounded_news.py"),
                                           "news_content.py": _sha(HERE / "news_content.py"),
                                           "../ner/matcher.py": _sha(HERE.parent / "ner/matcher.py")},
                           "max_tokens": MAX_TOKENS, "truncation": False, "probs_order": list(LABELS),
                           "classification_scope": LABEL_SCOPE, "device": device,
                           "evidence_validation": "mapped_full_article_or_legacy_excerpt_v1",
                           "local_files_only": True, "trained_or_finetuned_here": False}
        self.provenance_sha256 = _json_sha(self.provenance)

    def _runtime(self, role):
        if role not in self._runtimes:
            spec = self.specs[role]
            for name, expected in self.provenance["models"][role]["files_sha256"].items():
                if _sha(Path(spec["path"]) / name) != expected:
                    raise ValueError("Model/tokenizer files changed after predictor initialization")
            self._runtimes[role] = self._factory(spec, self.device)
        return self._runtimes[role]

    def predict(self, rows):
        results = [_prepare(row) for row in rows]
        for result in results:
            result["provenance_sha256"] = self.provenance_sha256
            result["model"] = None
        for language, role in LANGUAGE_ROLES.items():
            eligible = [result for result in results if result["status"] == "pending" and result["language"] == language]
            if not eligible:
                continue
            runtime = self._runtime(role)
            pending = []
            for result in eligible:
                result["model"] = {key: self.provenance["models"][role][key]
                                   for key in ("repo", "revision", "files_manifest_sha256")}
                lengths = runtime.token_lengths([context["actual_input"] for context in result["contexts"]])
                if len(lengths) != len(result["contexts"]) or any(type(n) is not int or n < 1 for n in lengths):
                    raise ValueError("Tokenizer returned invalid context lengths")
                for context, length in zip(result["contexts"], lengths):
                    context["tokens"] = length
                    if length > runtime.max_tokens:
                        context["status"] = "context_too_long"
                if any(n > runtime.max_tokens for n in lengths):
                    result["status"] = "context_too_long"
                else:
                    pending.extend(result["contexts"])
            for start in range(0, len(pending), self.batch_size):
                contexts = pending[start:start + self.batch_size]
                predictions = runtime.predict([context["actual_input"] for context in contexts])
                if len(predictions) != len(contexts):
                    raise ValueError("Model returned an incorrect number of predictions")
                for context, values in zip(contexts, predictions):
                    context["probabilities"] = _probabilities(values)
                    context["label"] = _label(context["probabilities"])
                    context["status"] = "analyzed" if context["label"] else "tied_probabilities"
            for result in eligible:
                if result["status"] != "pending":
                    continue
                labels = {context["label"] for context in result["contexts"]}
                if None in labels:
                    result["status"] = "tied_probabilities"
                    continue
                if {"POSITIVE", "NEGATIVE"} <= labels:
                    result["status"] = "conflicting_polarity"
                    continue
                probabilities = {label: sum(context["probabilities"][label] for context in result["contexts"])
                                 / len(result["contexts"]) for label in LABELS}
                label = _label(probabilities)
                if label is None:
                    result["status"] = "tied_probabilities"
                    continue
                result.update(status="analyzed", label=label, probabilities=probabilities,
                              score=probabilities["positive"] - probabilities["negative"],
                              model_max_probability=max(probabilities.values()))
        if any(result["status"] == "pending" for result in results):
            raise RuntimeError("Sentiment batch left pending rows")
        for result in results:
            for context in result["contexts"]:
                if context["status"] == "pending":
                    context.update(status="not_inferred", not_inferred_reason=result["status"])
        return results

    def close(self):
        self._runtimes.clear()
        gc.collect()
        if self.device.startswith("cuda"):
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
