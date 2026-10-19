#!/usr/bin/env python3
"""Emotion reading for drafts: which feeling a post carries, and each person's
emotional palette measured from their own writing.

**The model.** SamLowe/roberta-base-go_emotions (MIT), a RoBERTa fine-tuned on
Google's GoEmotions: 58k Reddit comments hand-labelled with 27 emotions plus
"neutral". The INT8 ONNX build runs on onnxruntime with no PyTorch, 125 MB.
Chosen on 2026-10-18 after reading the literature: fine-tuned classifiers beat
zero-shot LLMs, Claude Opus and GPT-4 included, at emotion classification
(arXiv 2406.08660, 2503.04831), and this is the strongest one we may ship.
Cardiff's tweet-trained model fits the domain better but carries no licence;
EmoLex and LIWC need paid commercial licences.

**What it is used for, and what it is not.** Measured on his reaction posts
against 59 authorless bangers, it reads emotion far better than a word list
(the self-driving-toilet post is "love" at 0.86, the copywriter rant
"annoyance") but as a pass/fail gate it is *not* better than critic.has_feeling:
it reads "approval" and "admiration" into drafts that praise their subject
("New Balance hasn't touched the 990 since 1982..."), which is the subject's
sentiment, not the author's. So it is used for two things only:

  1. Rescue. A draft the word list misses but the model reads as a clear,
     author-side feeling (neutral < 0.4, and not one of the subject-directed
     labels) counts as having one.
  2. Palette. Each person's emotional range, measured from the quoted lines of
     their own voice card, goes into the maker's and critic's prompts. A
     per-draft veto on it was tested and rejected: seventeen posts is too few,
     and it would have refused "No idea yet what the tape is" as off-palette
     "confusion". The real finding is feed-level: his posts lead with negative
     or uncertain feelings 8 times in 10, the engine's drafts led with
     approval, admiration or gratitude 10 times in 17. That relentless
     positivity is the tell, and the palette line is how the maker hears it.

**Shipping.** The model is not in git (125 MB is over GitHub's 100 MB file
limit). `fetch()` pulls it from Hugging Face into models/go_emotions/ and
checks every file against a pinned SHA-256, so a changed upstream file is
refused rather than silently loaded. Every caller degrades to None when the
packages or the model are missing: the engine then runs on the word list and
VADER exactly as it did before this module existed.
"""
import functools
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL_DIR = Path(os.environ.get("KARAMEL_EMOTION_MODEL_DIR")
                 or HERE.parent / "models" / "go_emotions")
REPO = "SamLowe/roberta-base-go_emotions-onnx"
BASE_URL = f"https://huggingface.co/{REPO}/resolve/main/onnx"
FILES = {
    "model_quantized.onnx": (
        "0c1981c5b479674747911c8e2228f0c4ec90bf47bf66e830f7d4fc62be082958", 125397543),
    "tokenizer.json": (
        "63735ef382776e869c0ee50f8e999ab19111bb794f8a451559e611077dfe7f25", 2108884),
    "config.json": (
        "60db0b3d640dedb02f9a033578ddecb78fa1e72ac67dd9d391606cba0e6cbcf1", 1888),
}
PACKAGES = ("onnxruntime", "tokenizers", "numpy")
MAX_TOKENS = 128

# The GoEmotions paper's own sentiment grouping of the 27 labels.
SENTIMENT = {
    "positive": {"amusement", "excitement", "joy", "love", "desire", "optimism",
                 "caring", "pride", "admiration", "gratitude", "relief",
                 "approval"},
    "negative": {"fear", "nervousness", "remorse", "embarrassment",
                 "disappointment", "sadness", "grief", "disgust", "anger",
                 "annoyance", "disapproval"},
    "ambiguous": {"realization", "surprise", "curiosity", "confusion"},
}
# Labels the model assigns to praise or recognition *of the subject*. They are
# real readings, just not evidence that the author felt anything.
SUBJECT_LABELS = frozenset({"approval", "admiration", "realization"})
RESCUE_NEUTRAL = 0.4
CARRIES_FEELING_NEUTRAL = 0.6


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_present():
    """Every file there at its pinned size. Hashes are checked on fetch, not
    on every load: hashing 125 MB per heartbeat is wasted work."""
    return all((MODEL_DIR / n).is_file() and (MODEL_DIR / n).stat().st_size == size
               for n, (_, size) in FILES.items())


def packages_present():
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
        import tokenizers  # noqa: F401
    except ImportError:
        return False
    return True


def fetch(quiet=False):
    """Download any missing or wrong-sized model file, verify, then move into
    place. Returns True when the model is complete and verified."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, (digest, size) in FILES.items():
        dest = MODEL_DIR / name
        if dest.is_file() and dest.stat().st_size == size and _sha256(dest) == digest:
            continue
        if not quiet:
            print(f"  fetching {name} ({size / 1e6:.1f} MB) from {REPO}")
        fd, tmp = tempfile.mkstemp(dir=str(MODEL_DIR), prefix=f".{name}.")
        os.close(fd)
        try:
            urllib.request.urlretrieve(f"{BASE_URL}/{name}", tmp)
            got = _sha256(tmp)
            if got != digest:
                raise ValueError(f"{name}: checksum {got[:12]} != pinned {digest[:12]}")
            os.replace(tmp, dest)
        except Exception as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if not quiet:
                print(f"  could not fetch {name}: {e}", file=sys.stderr)
            return False
    return True


@functools.lru_cache(maxsize=1)
def _runtime():
    """(session, tokenizer, labels) or None. Cached for the life of the process."""
    if not (packages_present() and model_present()):
        return None
    try:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        labels_map = json.loads((MODEL_DIR / "config.json").read_text())["id2label"]
        labels = [labels_map[str(i)] for i in range(len(labels_map))]
        tok = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
        tok.enable_truncation(max_length=MAX_TOKENS)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        sess = ort.InferenceSession(str(MODEL_DIR / "model_quantized.onnx"),
                                    sess_options=opts,
                                    providers=["CPUExecutionProvider"])
        return sess, tok, labels
    except Exception:
        return None


def available():
    return _runtime() is not None


def score(text):
    """{label: probability} over the 28 labels, or None when unavailable.
    Multi-label: each probability is an independent sigmoid, so they do not
    sum to one."""
    rt = _runtime()
    if rt is None or not (text or "").strip():
        return None
    import numpy as np

    sess, tok, labels = rt
    enc = tok.encode(text)
    ids = np.array([enc.ids], dtype=np.int64)
    mask = np.array([enc.attention_mask], dtype=np.int64)
    feeds = {i.name: (mask if "mask" in i.name else ids) for i in sess.get_inputs()}
    logits = sess.run(None, feeds)[0][0]
    probs = 1.0 / (1.0 + np.exp(-logits))
    return {lab: float(p) for lab, p in zip(labels, probs)}


def top(scores):
    """The strongest non-neutral label and its probability."""
    if not scores:
        return None, 0.0
    label = max((k for k in scores if k != "neutral"), key=lambda k: scores[k])
    return label, scores[label]


def sentiment_of(label):
    for group, members in SENTIMENT.items():
        if label in members:
            return group
    return "neutral"


def rescues(text):
    """True when the model reads a clear feeling on the author's side that a
    word list could miss. None when the model is unavailable."""
    s = score(text)
    if s is None:
        return None
    label, _ = top(s)
    return s["neutral"] < RESCUE_NEUTRAL and label not in SUBJECT_LABELS


def palette(texts):
    """A person's emotional range, from their own writing.

    Counts the leading emotion of every text that carries one, and the
    sentiment-group shares. None when the model is unavailable or no text
    carries a feeling."""
    if not available():
        return None
    leads = Counter()
    for t in texts:
        s = score(t)
        if s and s["neutral"] < CARRIES_FEELING_NEUTRAL:
            leads[top(s)[0]] += 1
    n = sum(leads.values())
    if not n:
        return None
    groups = Counter()
    for label, c in leads.items():
        groups[sentiment_of(label)] += c
    return {
        "n_texts": len(texts),
        "n_with_feeling": n,
        "leads": dict(leads.most_common()),
        "groups": {g: round(groups[g] / n, 2) for g in ("negative", "ambiguous", "positive")},
    }


def describe(pal, author="them"):
    """The palette as one line a model can follow."""
    if not pal:
        return ""
    g = pal["groups"]
    leads = ", ".join(list(pal["leads"])[:6])
    tilt = max(g, key=g.get)
    line = (f"Emotional palette, measured from {author}'s own writing "
            f"({pal['n_with_feeling']} of {pal['n_texts']} samples carry a feeling): "
            f"mostly {leads}. Sentiment mix: {int(g['negative'] * 100)}% negative, "
            f"{int(g['ambiguous'] * 100)}% uncertain or curious, "
            f"{int(g['positive'] * 100)}% positive.")
    if g["positive"] < 0.4:
        line += (" Machines default to approving, admiring and grateful; this "
                 "person mostly does not, so a warm or impressed draft needs a "
                 f"reason. Lean {tilt}.")
    return line


def read_samples(path):
    """The texts in a samples.jsonl, or [] when there is none."""
    path = Path(path) if path else None
    if not path or not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            t = json.loads(line).get("text")
        except (ValueError, AttributeError):
            continue
        if t:
            out.append(t)
    return out


@functools.lru_cache(maxsize=16)
def _palette_line(path_str, mtime, author):
    return describe(palette(read_samples(path_str)), author)


@functools.lru_cache(maxsize=16)
def _palette_dict(path_str, mtime):
    return palette(read_samples(path_str))


def palette_for(samples_path):
    """The palette dict for a samples file, cached until it changes."""
    p = Path(samples_path) if samples_path else None
    if not p or not p.is_file():
        return None
    return _palette_dict(str(p), p.stat().st_mtime)


DRIFT_WINDOW = 3
DRIFT_HITS = 2
RARE_SHARE = 0.25


def lead_group(text):
    """The sentiment group a text leads with, or None if it carries no feeling."""
    s = score(text)
    if s is None or s["neutral"] >= CARRIES_FEELING_NEUTRAL:
        return None
    return sentiment_of(top(s)[0])


def drift_avoid(recent_texts, pal, window=DRIFT_WINDOW, hits=DRIFT_HITS,
                rare=RARE_SHARE):
    """The sentiment group the next draft must not lead with, or None.

    Tuning after the 2026-10-18 audit: Adi's drafts led positive 47% of the
    time against 22% of his own writing. A group that is rare for this person
    (at most `rare` of their palette) and has led `hits` of the last `window`
    shipped drafts is drift, and the next draft is steered off it. Per person:
    for Adi that is "positive"; for Khoa, whose writing is 88% positive, it is
    "negative" or "ambiguous", and warmth is never penalised."""
    if not pal or not available():
        return None
    leads = [g for g in (lead_group(t) for t in recent_texts[-window:]) if g]
    for group, share in pal["groups"].items():
        if share <= rare and leads.count(group) >= hits:
            return group
    return None


def palette_line(samples_path, author="them"):
    """The palette sentence for a person, cached until their samples change.
    Empty when there is no samples file or no model."""
    p = Path(samples_path) if samples_path else None
    if not p or not p.is_file():
        return ""
    return _palette_line(str(p), p.stat().st_mtime, author)


def selftest():
    assert sentiment_of("annoyance") == "negative"
    assert sentiment_of("curiosity") == "ambiguous"
    assert sentiment_of("approval") == "positive"
    assert top({"neutral": 0.9, "joy": 0.2, "anger": 0.1}) == ("joy", 0.2)
    assert top(None) == (None, 0.0)
    import tempfile as _tf
    _p = Path(_tf.mkdtemp()) / "samples.jsonl"
    _p.write_text('{"text": "one"}\nnot json\n{"text": ""}\n{"text": "two"}\n')
    assert read_samples(_p) == ["one", "two"]
    assert read_samples(None) == [] and palette_line(None) == ""
    assert describe(None) == ""
    line = describe({"n_texts": 10, "n_with_feeling": 5,
                     "leads": {"annoyance": 3, "curiosity": 2},
                     "groups": {"negative": 0.6, "ambiguous": 0.4, "positive": 0.0}},
                    "Adi")
    assert "annoyance, curiosity" in line and "approving" in line
    assert drift_avoid(["a", "b"], None) is None
    if available():
        adi_like = {"groups": {"negative": 0.56, "ambiguous": 0.22, "positive": 0.22}}
        warm = ["Someone raised money to build a toilet that drives itself to you. "
                "Whatever it is. I love it."] * 2
        assert drift_avoid(warm, adi_like) == "positive"
        khoa_like = {"groups": {"negative": 0.0, "ambiguous": 0.12, "positive": 0.88}}
        assert drift_avoid(warm, khoa_like) is None, "warmth is Khoa's, never drift"
        receipt = score("Just paid $7 for a Crumbl cookie that will be gone "
                        "from the menu next week.")
        assert receipt["neutral"] > 0.6, receipt["neutral"]  # 0.71 / 0.89
        assert top(score("Someone raised money to build a toilet that drives "
                         "itself to you. Whatever it is. I love it."))[0] == "love"
        # Only cases with a wide margin. The INT8 model's raw numbers differ by
        # chip: on 2026-10-18 the Crumbl receipt read 0.89 neutral on an Apple
        # M-series Mac (onnxruntime 1.31) and 0.71 on the Intel host (1.19.2),
        # and "smh" went from 0.39 to 0.48, which flips a 0.4 threshold. The
        # gate's decisions did not move on either machine (10/10 of his
        # reaction posts, 0/59 authorless bangers), and these assertions are
        # chosen to hold on both.
        assert rescues("Someone raised money to build a toilet that drives "
                       "itself to you. Whatever it is. I love it.") is True
        assert rescues("Just paid $7 for a Crumbl cookie that will be gone "
                       "from the menu next week.") is False
        print("emotion selftest: all assertions passed (model loaded)")
    else:
        print("emotion selftest: all assertions passed (model not installed, "
              "pure checks only)")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return 0
    if "--fetch" in sys.argv:
        ok = fetch()
        print("model ready" if ok else "model not fetched")
        return 0 if ok else 1
    if "--status" in sys.argv:
        print(f"packages: {'yes' if packages_present() else 'missing ' + ' '.join(PACKAGES)}")
        print(f"model:    {'yes' if model_present() else 'missing'} ({MODEL_DIR})")
        print(f"ready:    {'yes' if available() else 'no'}")
        return 0
    text = " ".join(a for a in sys.argv[1:] if not a.startswith("--"))
    s = score(text)
    if s is None:
        print("emotion model unavailable: run with --status", file=sys.stderr)
        return 2
    for label, p in sorted(s.items(), key=lambda kv: -kv[1])[:6]:
        print(f"{label:>15} {p:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
