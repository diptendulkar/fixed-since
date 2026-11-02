"""Render findings as a Markdown report with draft replies for a maintainer."""

from __future__ import annotations

from fixed_since.finder import Candidate
from fixed_since.github import Issue

_EVIDENCE_LABEL = {
    "claims-fix": "the pull request says it fixes this issue",
    "partial": "the pull request says it is part of, or related to, this issue",
    "title-search": "title search only; check the diff",
    "reopened-after": "the issue was reopened after this merged, so it was not enough",
    "mention": "the pull request only mentions this issue",
}

DRAFTABLE = ("claims-fix", "partial")


def draft_reply(candidate: Candidate) -> str | None:
    """A reply that cites its evidence and asks rather than declares.

    Returns ``None`` when the evidence is too weak to say anything to the
    reporter; a mention or a search match is for the maintainer to check.
    """
    if candidate.evidence not in DRAFTABLE:
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

    if candidate.evidence == "claims-fix":
        return "\n".join(
            [
                f"This looks like it was fixed by #{pull.number} ({pull.title}), "
                f"merged on {merged}. {shipped}",
                "",
                f"If you still see this{since}, could you say which version you're running? "
                "Otherwise this can probably be closed.",
            ]
        )
    return "\n".join(
        [
            f"Work toward this landed in #{pull.number} ({pull.title}), merged on {merged}. "
            f"{shipped}",
            "",
            f"That pull request describes itself as part of this issue rather than a full fix, "
            f"so: does what's available{since} cover what you asked for, or is something "
            "still missing?",
        ]
    )


def _candidate_line(candidate: Candidate) -> str:
    pull = candidate.pull
    shipped = candidate.release.tag if candidate.release else "unreleased"
    return (
        f"- [#{pull.number}]({pull.url}) {pull.title}: merged "
        f"{pull.merged_at.date().isoformat()}, {shipped}. "
        f"Evidence: {_EVIDENCE_LABEL[candidate.evidence]}."
    )


def render_report(results: list[tuple[Issue, list[Candidate]]]) -> str:
    likely = [(i, c) for i, c in results if c and c[0].evidence in DRAFTABLE]
    weak = [(i, c) for i, c in results if c and c[0].evidence not in DRAFTABLE]

    out = [f"# fixed-since: {len(likely)} of {len(results)} issues have a likely fix", ""]
    for issue, candidates in likely:
        out.append(f"## [#{issue.number}]({issue.url}) {issue.title}")
        out.append(f"Opened {issue.created_at.date().isoformat()}.")
        out.append("")
        out.extend(_candidate_line(candidate) for candidate in candidates)
        out.append("")
        out.append("Draft reply:")
        out.append("")
        reply = draft_reply(candidates[0]) or ""
        out.extend(f"> {line}" if line else ">" for line in reply.splitlines())
        out.append("")

    if weak:
        out.append("## Weak matches, no draft")
        out.append("")
        out.append("These have related merged pull requests, but none is a likely fix.")
        out.append("")
        for issue, candidates in weak:
            out.append(f"### [#{issue.number}]({issue.url}) {issue.title}")
            out.extend(_candidate_line(candidate) for candidate in candidates)
            out.append("")
    return "\n".join(out)
