"""Recognise and rewrite private tracker references in published content.

The public mirror carries no GitHub issues and no pull requests, so every
pointer to a tracker number is dead there. This module holds the rules
that tell a tracker reference from a number that counts something else,
and the wording that replaces a reference.

Two signals name a tracker number:

* a URL that points at a tracker entry, such as a link to an issue or a
  pull request;
* a tracker word that introduces a number, such as "issue #<number>" or
  "PR #<number>".

A bare number is a tracker reference only when the same repository
history names that number as a tracker entry somewhere else. A number the
history never names that way counts something else: a tier ("T4 #7"), a
standard ("Standards #14"), a row, a hex colour, an HTML entity. A number
that both a tracker word and an internal word introduce keeps the internal
reading, because the internal reading names something in this repository.

The wording follows the review the working tree already went through. A
tracker word keeps its role in the sentence and only its number goes:

    "Map: wayfinder #<number>."         -> "Map: the wayfinder program."
    "Issue #<number> remains the gate." -> "The tracked change remains the gate."

A link whose label is only a number keeps a phrase that reads as a noun
phrase, and a link whose label names something keeps that name.
"""

from __future__ import annotations

import re

# Words that introduce a tracker entry in prose.
#
# A word only counts when the number it introduces is a number the
# repository history names as a tracker entry. "Ticket 2" in a comment
# about a human-in-the-loop ticket is not a tracker reference, and
# "merged 26" counts rows.
TRACKER_WORDS = (
    "issue",
    "issues",
    "pr",
    "prs",
    "pull",
    "pulls",
    "pull request",
    "pull requests",
    "wayfinder",
    "github map",
    "github tracer",
    "tracer",
    "iknow-memory",
    "iknow",
)

# Words that introduce a number that counts something in this repository
# rather than a tracker entry. A number right after one of these keeps
# its internal reading even when the history names that number as a
# tracker entry elsewhere, because the internal reading is the one that
# names something here.
#
# The review the working tree went through draws the line this way: a
# tier or a standard names a local thing, so "T4 #138" and "Standards #4"
# keep their internal reading, while a number that only a tracker word
# introduces goes.
INTERNAL_WORDS = (
    "row",
    "run",
    "seam",
    "step",
    "item",
    "line",
    "part",
    "tier",
    "wave",
    "stage",
    "phase",
    "block",
    "slot",
    "index",
    "case",
    "test",
    "task",
    "standards",
    "standard",
    "spec",
    "code-review",
    "citation",
    "t1",
    "t2",
    "t3",
    "t4",
    "t5",
    "t6",
    "t7",
    "t8",
    "t9",
    "t10",
)

# Words whose number is a tracker number even when the word looks local.
# The review removed the number from "T4 (#138)" and from "T1 (#33)", and
# kept "Standards #4" and "Spec #5", so a tier loses its number while a
# standard keeps it.
TIER_WORDS = (
    "t1",
    "t2",
    "t3",
    "t4",
    "t5",
    "t6",
    "t7",
    "t8",
    "t9",
    "t10",
)

# The wording that replaces a reference that carries no wording of its own.
GENERIC_ISSUE_NAME = "the tracked change"
GENERIC_PR_NAME = "the tracked pull request"

# The wording that replaces a tracker word and its number. The whole
# phrase goes, because the word alone no longer names anything once the
# number is gone. The wording follows the review the working tree already
# went through.
WAYFINDER_NAME = "the wayfinder program"
PULL_WORD_NAME = "that pull request"
ISSUE_WORD_NAME = "the tracked change"

# A markdown inline link. The label may span lines, because a long label
# in a paragraph is wrapped in the source.
MARKDOWN_LINK = re.compile(
    r"\[(?P<label>[^\]\n]*(?:\n[^\]\n]*)*)\]\((?P<url>[^)\s]*)\)"
)

# A tracker entry named by a URL.
TRACKER_URL = re.compile(
    r"https?://[^\s)<>\"']*?/(?:issues|pull)/(?P<number>\d{1,4})\b"
)

# A link to a file at a revision, such as a link to a blob or a tree. The
# link names a tracker entry by its revision rather than by its number.
REVISION_LINK = re.compile(
    r"https?://[^\s)<>\"']*?/(?:blob|tree)/[0-9a-f]{7,40}(?:/[^\s)<>\"']*)?"
)

# A tracker entry named by a word and a number. The number ends at a
# word boundary, so a commit hash such as "65f2f9f" is not read as the
# number 65. The word and the number share a line, so a list item that
# follows a phrase is not read as that phrase's number.
WORD_TRACKER_REF = re.compile(
    r"(?P<word>\b(?:" + "|".join(re.escape(w) for w in TRACKER_WORDS) + r")\b)"
    r"[ \t]*"
    r"(?P<hash>#)?"
    r"(?P<number>\d{1,4})(?![0-9A-Za-z_])",
    re.IGNORECASE,
)

# A number with a leading hash and nothing else. A chain of numbers such
# as "#238/#243" is matched one number at a time.
BARE_HASH_NUMBER = re.compile(r"#(?P<number>\d{1,4})\b")

# A hash that is an HTML entity, such as &#160;.
HTML_ENTITY = re.compile(r"&#\d+;")

# A word that stands right before a hash. The pattern is matched against
# the text that ends at the hash.
WORD_BEFORE_HASH = re.compile(r"(?P<word>[A-Za-z][A-Za-z0-9_.-]*)\s*$")

# A range of numbers, such as #74-#78.
NUMBER_RANGE = re.compile(
    r"#(?P<first>\d{1,4})\s*[-–—~]\s*#?(?P<second>\d{1,4})"
)

# A sub-item of a number, such as #318.1.
SUB_ITEM = re.compile(r"#(?P<number>\d{1,4})\.(?P<sub>\d+)\b")

# The tracker words that name a pull request rather than an issue.
PULL_WORDS = ("pr", "prs", "pull", "pulls", "pull request", "pull requests")

# The tracker words that name a program rather than an entry.
PROGRAM_WORDS = ("wayfinder", "github map", "github tracer", "tracer")


def word_is_internal(word: str) -> bool:
    """Whether a word introduces a number that counts something local."""
    return word.lower() in INTERNAL_WORDS


def strip_number_from_label(label: str, number: int | None) -> str:
    """Drop a tracker number from a link label."""
    if number is None:
        return label
    cleaned = re.sub(rf"#{number}\b", "", label)
    cleaned = re.sub(
        rf"\b(?:{'|'.join(re.escape(w) for w in TRACKER_WORDS)})\s*#?\s*{number}\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # A label that was only a number leaves its separator behind, such as
    # the dash in "#411 - archive restore state machine".
    cleaned = re.sub(r"^[\s—–\-:]+|[\s—–\-:]+$", "", cleaned)
    return cleaned.strip()


def collect_known_numbers(texts: list[str]) -> set[int]:
    """Every number that the given texts name as a tracker entry."""
    numbers: set[int] = set()
    for text in texts:
        for match in TRACKER_URL.finditer(text):
            numbers.add(int(match.group("number")))
        for match in WORD_TRACKER_REF.finditer(text):
            if match.group("number"):
                numbers.add(int(match.group("number")))
    return numbers


def _replacement_for_word(word: str) -> str:
    """The wording that replaces a tracker word and its number."""
    lowered = word.lower()
    if lowered in PROGRAM_WORDS:
        return WAYFINDER_NAME
    if lowered in PULL_WORDS:
        return PULL_WORD_NAME
    if lowered in ("issue", "issues"):
        return ISSUE_WORD_NAME
    return GENERIC_ISSUE_NAME


def scrub_text(
    text: str,
    known: set[int],
    private_org: str = "",
    private_repo: str = "",
    private_hashes: tuple[str, ...] = (),
) -> tuple[str, int]:
    """Scrub tracker references out of a text.

    The rules run in a fixed order. A markdown link is handled before the
    bare-number rule, so a link's own label is not counted twice. A
    number that a word introduces is handled before the bare rule, so the
    word's own reading decides.

    The private organisation, repository and commit names are named by
    the caller. They are not written into this module, because this
    module is published and the names are internal.
    """
    count = 0

    def link_replacement(match: re.Match) -> str:
        nonlocal count
        url = match.group("url")
        label = match.group("label")
        url_match = TRACKER_URL.search(url)
        if not url_match:
            # A link to a file at a revision names the private repository
            # without naming a number. The link goes; the label stays as
            # plain prose, so the sentence still reads.
            if private_org and private_org.lower() in url.lower():
                count += 1
                cleaned_label = label.strip()
                if not cleaned_label:
                    return GENERIC_ISSUE_NAME
                return cleaned_label
            return match.group(0)
        number = int(url_match.group("number"))
        is_pull = "/pull/" in url
        cleaned = strip_number_from_label(label, number)
        if not cleaned:
            cleaned = GENERIC_PR_NAME if is_pull else GENERIC_ISSUE_NAME
        count += 1
        return cleaned

    text = MARKDOWN_LINK.sub(link_replacement, text)

    def revision_replacement(match: re.Match) -> str:
        nonlocal count
        url = match.group(0)
        if private_org and private_org.lower() not in url.lower():
            return match.group(0)
        count += 1
        return ""

    text = REVISION_LINK.sub(revision_replacement, text)

    def url_replacement(match: re.Match) -> str:
        nonlocal count
        count += 1
        return GENERIC_PR_NAME if "/pull/" in match.group(0) else GENERIC_ISSUE_NAME

    text = TRACKER_URL.sub(url_replacement, text)

    def word_replacement(match: re.Match) -> str:
        nonlocal count
        number = match.group("number")
        if not number:
            return match.group(0)
        count += 1
        return _replacement_for_word(match.group("word"))

    text = WORD_TRACKER_REF.sub(word_replacement, text)

    def range_replacement(match: re.Match) -> str:
        nonlocal count
        first = int(match.group("first"))
        second = int(match.group("second"))
        if first not in known and second not in known:
            return match.group(0)
        count += 1
        return f"{GENERIC_ISSUE_NAME} through {GENERIC_ISSUE_NAME}"

    text = NUMBER_RANGE.sub(range_replacement, text)

    def sub_item_replacement(match: re.Match) -> str:
        nonlocal count
        number = int(match.group("number"))
        if number not in known:
            return match.group(0)
        count += 1
        return f"{GENERIC_ISSUE_NAME} step {match.group('sub')}"

    text = SUB_ITEM.sub(sub_item_replacement, text)

    def bare_replacement(match: re.Match) -> str:
        nonlocal count
        number = int(match.group("number"))
        if number not in known:
            return match.group(0)
        start = match.start()
        if HTML_ENTITY.search(text[max(0, start - 1) : start + 1]):
            return match.group(0)
        before = text[max(0, start - 60) : start]
        internal = WORD_BEFORE_HASH.search(before)
        if internal and word_is_internal(internal.group("word")):
            return match.group(0)
        count += 1
        return GENERIC_ISSUE_NAME

    text = BARE_HASH_NUMBER.sub(bare_replacement, text)

    if private_org and private_repo:
        slug = f"{private_org}/{private_repo}"
        count += text.count(slug)
        text = text.replace(slug, "the private repository")

    if private_org:
        count += text.count(private_org)
        text = text.replace(private_org, "the private organisation")

    for commit_hash in private_hashes:
        if commit_hash:
            count += text.count(commit_hash)
            text = text.replace(commit_hash, "a private commit")

    return text, count