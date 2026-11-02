"""Render findings as a Markdown report with draft replies for a maintainer."""

from __future__ import annotations

from typing import Literal

from fixed_since.finder import SEARCH_EVIDENCE, Candidate
from fixed_since.github import Issue

_EVIDENCE_LABEL = {
    "claims-fix": "the pull request says it fixes this issue",
    "partial": "the pull request says it is part of, or related to, this issue",
    "title-search": "title search only; check the diff",
    "model-search": "found by a model-suggested title search; check the diff",
    "reopened-after": "the issue was reopened after this merged, so it was not enough",
    "mention": "the pull request only mentions this issue",
}

DraftKind = Literal["fixed", "ask"]


def draft_kind(candidate: Candidate) -> DraftKind | None:
    """What, if anything, the evidence lets us say to the reporter.

    The pull request's own wording sets the ceiling. A model's reading of the
    diff can lower it, and can lift a title-search match only as far as a
    question, never to a claim that the issue is fixed.
    """
    outcome = candidate.verdict.outcome if candidate.verdict else None
    if candidate.evidence in ("mention", "reopened-after") or outcome == "unrelated":
        return None
    if candidate.evidence in SEARCH_EVIDENCE:
        return "ask" if outcome in ("addresses", "partial") else None
    if candidate.evidence == "partial" or outcome == "partial":
        return "ask"
    return "fixed"


def draft_reply(candidate: Candidate) -> str | None:
    """A reply that cites its evidence and asks rather than declares."""
    kind = draft_kind(candidate)
    if kind is None:
        return None
    pull = candidate.pull
    merged = pull.merged_at.date().isoformat()
    release = candidate.release
    shipped = (
        f"It first shipped in [{release.tag}]({release.url})."
        if release is not None
        else "It's merged but not in a release yet, so it should be in the next one."
    )
    since = f" on {release.tag} or later" if release is not None else " once that's released"
    cited = f"#{pull.number} ({pull.title}), merged on {merged}"

    if kind == "fixed":
        opening = f"This looks like it was fixed by {cited}. {shipped}"
        ask = (
            f"If you still see this{since}, could you say which version you're running? "
            "Otherwise this can probably be closed."
        )
    elif candidate.evidence in SEARCH_EVIDENCE:
        opening = f"{cited} changed this area and may cover it. {shipped}"
        ask = (
            f"Could you check whether things work for you{since}, or whether something is missing?"
        )
    else:
        opening = f"Work toward this landed in {cited}. {shipped}"
        ask = (
            f"It looks like part of this rather than a full fix, so: does what's available{since} "
            "cover what you asked for, or is something still missing?"
        )
    return f"{opening}\n\n{ask}"


def _candidate_lines(candidate: Candidate) -> list[str]:
    pull = candidate.pull
    shipped = candidate.release.tag if candidate.release else "unreleased"
    lines = [
        f"- [#{pull.number}]({pull.url}) {pull.title}: merged "
        f"{pull.merged_at.date().isoformat()}, {shipped}. "
        f"Evidence: {_EVIDENCE_LABEL[candidate.evidence]}."
    ]
    if candidate.verdict is not None:
        lines.append(f"  - Diff check: **{candidate.verdict.outcome}**. {candidate.verdict.reason}")
    return lines


def _best_draftable(candidates: list[Candidate]) -> Candidate | None:
    return next((c for c in candidates if draft_kind(c) is not None), None)


def render_report(results: list[tuple[Issue, list[Candidate]]]) -> str:
    likely = [(i, c) for i, c in results if _best_draftable(c) is not None]
    weak = [(i, c) for i, c in results if c and _best_draftable(c) is None]

    noun = (
        "discussions" if results and all(i.kind == "discussion" for i, _ in results) else "issues"
    )
    out = [f"# fixed-since: {len(likely)} of {len(results)} {noun} have a likely fix", ""]
    for issue, candidates in likely:
        out.append(f"## [#{issue.number}]({issue.url}) {issue.title}")
        out.append(f"Opened {issue.created_at.date().isoformat()}.")
        out.append("")
        for candidate in candidates:
            out.extend(_candidate_lines(candidate))
        out.append("")
        out.append("Draft reply:")
        out.append("")
        best = _best_draftable(candidates)
        reply = (draft_reply(best) if best else None) or ""
        out.extend(f"> {line}" if line else ">" for line in reply.splitlines())
        out.append("")

    if weak:
        out.append("## Weak matches, no draft")
        out.append("")
        out.append(
            "These have related merged pull requests, but none is a likely fix. "
            "Search matches stay here unless --verify finds the diff relevant."
        )
        out.append("")
        for issue, candidates in weak:
            out.append(f"### [#{issue.number}]({issue.url}) {issue.title}")
            for candidate in candidates:
                out.extend(_candidate_lines(candidate))
            out.append("")

    unmatched = [issue for issue, candidates in results if not candidates]
    if unmatched:
        out.append("## Nothing found")
        out.append("")
        out.extend(f"- [#{issue.number}]({issue.url}) {issue.title}" for issue in unmatched)
        out.append("")
    return "\n".join(out)
