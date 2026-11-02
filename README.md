# fixed-since

Find open GitHub issues that a later merged pull request already fixed, and draft a reply that cites the evidence, for a maintainer to review.

Large projects collect issues whose cause was fixed months ago. Nobody tells the person who reported it: the fix landed under a different issue, or the PR never mentioned theirs. The issue stays open, the reporter assumes it's still broken, and drive-by replies guess at an answer.

`fixed-since` finds those issues and shows its work.

## Principles

- **It never posts.** It's read-only and prints a report. A human decides what to say.
- **Every claim is cited.** Each suggestion names the pull request, its merge date, and the first release that contains it.
- **A mention is not a fix.** Most pull requests that link to an issue only mention it, for example in a table or as "adjacent work". Replies are drafted only when the pull request says it fixes the issue, or is part of fixing it.
- **A reopen overrules a claim.** If someone reopened the issue after a pull request merged, that pull request wasn't enough, whatever its description says.
- **Drafts ask rather than declare.** "If you still see this on v0.10.0 or later, could you say which version you're running?"

## Install

```bash
pipx install git+https://github.com/diptendulkar/fixed-since
```

It authenticates with `GITHUB_TOKEN` / `GH_TOKEN`, or with your GitHub CLI login if `gh` is installed. Public repositories also work without a token, at a much lower rate limit.

## Usage

```bash
fixed-since check OWNER/NAME 68              # one issue
fixed-since scan OWNER/NAME --limit 20       # the 20 oldest open issues
fixed-since scan OWNER/NAME --no-search      # linked pull requests only, no title search
```

## Checking the diff with a model (optional)

Wording is only a first filter: pull request descriptions overclaim. With `--verify`, a model reads each promising pull request's diff and says whether it **addresses** the issue, addresses it **partially**, is **unrelated**, or whether it's **unsure**.

```bash
# Any Chat Completions API: OpenAI, OpenRouter, or a local model via Ollama / LM Studio / vLLM
OPENAI_BASE_URL=http://localhost:11434/v1 fixed-since check OWNER/NAME 68 --verify openai --model qwen2.5-coder

ANTHROPIC_API_KEY=... fixed-since scan OWNER/NAME --verify anthropic --model <model>

# See exactly what would be sent, without calling any model
fixed-since check OWNER/NAME 68 --show-prompt
```

What the model can and can't do:

- **It can lower a draft, but can't raise one to "fixed".** "Unrelated" removes the draft from a pull request that says "Fixes #N". The furthest it can lift a title-search match is a question to the reporter, never a claim that the issue is fixed.
- **Unsupported answers count as "unsure".** Not citing a changed file, citing a file that isn't in the diff, or giving malformed JSON all fall back to the wording-based result.
- **The code goes first.** Large diffs are trimmed to fit: source files go in smallest first, then tests, then docs, with lockfiles and generated files last. A source file that doesn't fit is truncated rather than dropped. The prompt lists everything it left out, so the model knows what it hasn't seen.
- Only the three strongest candidates per issue are checked, and never mentions or pull requests overruled by a reopen.

## How it works

1. **Linked pull requests.** Reads the issue's timeline for merged pull requests in the same repository that reference it and were merged after it was opened.
2. **Grade the reference.** Reads how each pull request refers to the issue: "Fixes #N" claims a fix, "Part of #N" or "Related to #N" claims partial progress, and anything else is a mention. A pull request merged before the issue was last reopened is marked as not enough.
3. **Title search.** Only when no linked pull request claims a fix or progress: searches merged pull requests for the most specific words in the issue title. These matches are never drafted.
4. **First release.** Finds the earliest published, non-prerelease release after the merge whose tag contains the merge commit.
5. **Draft.** Writes a reply citing the pull request and release. A partial fix gets a question ("is something still missing?") instead of a suggestion to close.

## What it found on a real repository

Scanning the 25 oldest open issues of a large open-source agent platform turned up:

- An issue whose fix was in a pull request that said "Closes" it and shipped two months earlier. The issue had been **reopened** because the fix didn't hold. A naive tool would have asked the reporter to close it. `fixed-since` lists it as not enough.
- A feature request with three merged pull requests describing themselves as "Part of" it, all shipped. It gets a draft asking whether anything is still missing, not a claim that it's done.
- Two issues that merged pull requests only mentioned, one in a triage priority table and one as "adjacent areas". These are listed without a draft.

## Limitations

- Each issue costs several API calls (timeline, each linked pull request, release comparisons), so a scan of 25 issues takes about a minute. The title search uses GitHub's search API, which allows 30 requests a minute.
- Without `--verify`, reference grading reads wording, not code. It's a filter that decides what's worth a human's time, not a verdict. With `--verify`, a model's verdict is still for the maintainer to weigh, which is why it's shown next to the draft rather than hidden in it.
- `--verify` sends issue text, PR descriptions and diffs to the model provider you choose. For a private repository, use a local model.

## Roadmap

- GitHub Discussions (Q&A) as well as issues
- A GitHub Action that posts the report as a weekly maintainer digest, still never replying on its own
- Flag accidental closing keywords, such as "which closed #123" in a PR description

## License

MIT
