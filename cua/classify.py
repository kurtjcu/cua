"""Heuristic classifiers: dev-cycle phase, rework/correction signals, prompt fingerprints + clustering.

These are deliberately cheap and transparent (keyword rules, n-gram Jaccard). The agent personas do the
nuanced reading on top of the clustered, redacted output.
"""
from __future__ import annotations

import re
from collections import Counter

PHASES = [
    ("debugging", r"\b(error|exception|traceback|stack ?trace|fails?|failing|broken|bug|crash|doesn'?t work|not working|why (is|does)|fix)\b"),
    ("testing", r"\b(tests?|pytest|jest|vitest|unit test|e2e|coverage|tdd|assert|spec)\b"),
    ("deployment_ops", r"\b(deploy|release|ci|cd|pipeline|github actions|docker|k8s|kubernetes|terraform|cdk|cloudformation|aws|lambda|ecs|prod(uction)?|staging|infra|monitor|logs?)\b"),
    ("review_refactor", r"\b(refactor|clean ?up|review|simplif|rename|restructure|tidy|lint|optimi[sz]e|performance|pr\b|pull request)\b"),
    ("documentation", r"\b(readme|docs?|documentation|comment|changelog|explain (the|this) code|write ?up)\b"),
    ("planning_design", r"\b(plan|design|architect|approach|spec|requirements?|roadmap|trade-?offs?|should (i|we)|options for)\b"),
    ("scaffolding", r"\b(scaffold|bootstrap|set ?up|init(ialise|ialize)?|new (project|repo|app)|boilerplate|template|create a (new )?(project|app|repo))\b"),
    ("research", r"\b(deep ?dive|landscape|alternatives to|what is|how does|compare|research|difference between|best way|look up|find out|which (library|tool))\b"),
    ("implementation", r"\b(implement|add|build|write|create|make|feature|function|endpoint|component|support for|integrate)\b"),
]
_PHASE_RE = [(p, re.compile(rx, re.I)) for p, rx in PHASES]
LEAD_VERBS = {
    **dict.fromkeys(["plan", "design", "architect", "spec", "outline"], "planning_design"),
    **dict.fromkeys(["fix", "debug", "diagnose", "investigate", "why"], "debugging"),
    **dict.fromkeys(["test", "verify"], "testing"),
    **dict.fromkeys(["deploy", "release", "ship", "provision"], "deployment_ops"),
    **dict.fromkeys(["refactor", "review", "simplify", "clean", "optimise", "optimize", "tidy"], "review_refactor"),
    **dict.fromkeys(["document", "explain", "summarise", "summarize"], "documentation"),
    **dict.fromkeys(["scaffold", "bootstrap", "init", "setup"], "scaffolding"),
    **dict.fromkeys(["implement", "add", "build"], "implementation"),
    **dict.fromkeys(["research", "compare", "evaluate"], "research"),
}

CORRECTION_RE = re.compile(
    r"^(no\b|nope|wrong|that'?s not|not what i|you (didn'?t|missed|forgot|broke)|still (broken|failing|not)|"
    r"same (error|issue|problem)|didn'?t work|doesn'?t work|try again|revert|undo|go back|stop\b|wait\b|actually\b)",
    re.I,
)
CONTINUE_RE = re.compile(r"^(continue|go on|keep going|carry on|proceed|yes|yep|ok(ay)?|do it|go ahead|sounds good|lgtm)\W*$", re.I)
CONTEXT_REEXPLAIN_RE = re.compile(r"\b(as i (said|mentioned)|like i said|again,|i already|remember (that|when)|from (the )?(last|previous) (chat|session))\b", re.I)


def phase_of(text: str, tool_names: list[str] | None = None) -> str:
    t = (text or "")[:2000]
    if t.startswith("/"):
        cmd = t.split()[0].lower()
        if any(k in cmd for k in ("review", "pr")):
            return "review_refactor"
        if "test" in cmd:
            return "testing"
        if any(k in cmd for k in ("deploy", "release")):
            return "deployment_ops"
        if "plan" in cmd:
            return "planning_design"
    scores = Counter()
    for phase, rx in _PHASE_RE:
        n = len(rx.findall(t))
        if n:
            scores[phase] += n
    # the leading verb states intent more reliably than nouns later in the prompt
    first = re.match(r"\W*(?:(?:ok(?:ay)?|so|hey|now|please|can you|could you|let'?s)\W+)*(\w+)", t.lower())
    if first:
        lead = LEAD_VERBS.get(first.group(1))
        if lead:
            scores[lead] += 2
    # pasted stack traces are a strong debugging signal
    if re.search(r"Traceback \(most recent call last\)|^\s+at \S+ \(|Error: |error\[E\d+\]", t, re.M):
        scores["debugging"] += 3
    if not scores:
        if tool_names and any(n in ("Edit", "Write", "MultiEdit") for n in tool_names):
            return "implementation"
        return "other"
    return scores.most_common(1)[0][0]


def is_correction(text: str) -> bool:
    return bool(CORRECTION_RE.search((text or "").strip()[:200]))


def is_bare_continue(text: str) -> bool:
    return bool(CONTINUE_RE.match((text or "").strip()[:60]))


def reexplains_context(text: str) -> bool:
    return bool(CONTEXT_REEXPLAIN_RE.search(text or ""))


# ---------- prompt fingerprinting ----------
_NORMALISERS = [
    (re.compile(r"```[\s\S]*?```"), " <CODE> "),
    (re.compile(r"(Traceback[\s\S]{0,4000}?)(\n\n|$)"), " <TRACE> "),
    (re.compile(r"https?://\S+"), " <URL> "),
    (re.compile(r"(?:~|\.{0,2})?/[\w.\-/]+"), " <PATH> "),
    (re.compile(r"\b[\w\-]+\.(py|ts|tsx|js|json|md|yaml|yml|toml|go|rs|cs|cpp|h|sh)\b"), " <FILE> "),
    (re.compile(r"\b\d+(\.\d+)*\b"), " <N> "),
    (re.compile(r"[^\w<>\s]"), " "),
]
STOP = set("the a an to of and or in on for with is it this that i you me my we our be can please just so do".split())


def normalise(text: str) -> list[str]:
    t = (text or "")[:1500]
    for rx, rep in _NORMALISERS:
        t = rx.sub(rep, t)
    return [w for w in t.lower().split() if w not in STOP][:40]


def _shingles(tokens: list[str], n: int = 2) -> set[tuple]:
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def cluster_prompts(prompts: list[tuple[str, dict]], threshold: float = 0.35, max_items: int = 20000) -> list[dict]:
    """Greedy leader clustering on bigram Jaccard over the first ~15 normalised tokens.

    prompts: list of (text, meta). Returns clusters sorted by size with representative + metas.
    """
    clusters: list[dict] = []
    index: dict[tuple, list[int]] = {}  # first bigram -> cluster ids, to keep it roughly linear
    for text, meta in prompts[:max_items]:
        toks = normalise(text)
        if not toks:
            continue
        head = toks[:15]
        sh = _shingles(head)
        cand_ids = set()
        for g in list(sh)[:6]:
            cand_ids.update(index.get(g, []))
        best, best_j = None, 0.0
        for cid in cand_ids:
            c = clusters[cid]
            inter = len(sh & c["sh"])
            if not inter:
                continue
            j = inter / len(sh | c["sh"])
            if j > best_j:
                best, best_j = cid, j
        if best is not None and best_j >= threshold:
            clusters[best]["members"].append(meta)
        else:
            cid = len(clusters)
            clusters.append({"sh": sh, "rep_tokens": head, "rep": text, "members": [meta]})
            for g in sh:
                index.setdefault(g, []).append(cid)
    out = []
    for c in clusters:
        out.append({"size": len(c["members"]), "signature": " ".join(c["rep_tokens"][:12]), "rep": c["rep"], "members": c["members"]})
    out.sort(key=lambda c: -c["size"])
    return out
