"""Two columns that did not mean what they were named.

Both bugs had the same shape, in the same block of `lead_engine.py`, and both
produced wrong emails rather than errors -- which is why nothing caught them:

1. `emails_extra` was assigned `intelligence["phones_found"]`. A column named
   for email addresses held `["(512) 661-7896", "1750990834", ...]`, and
   `leads_read.decoded()` then JSON-decoded it AS emails.

2. `weakness` was assigned the scorer's `reasons`. But llm.py asks for reasons
   as *"short strings citing concrete evidence and sub-scores"* -- for a strong
   lead that evidence is praise. The drafting prompt then printed it under
   "WHY THEY MAY NEED THIS". The model did not report the contradiction; it
   resolved it by inventing a defect. A lead scored STRONG_DIGITAL_PRESENCE
   (presence=100) got an email saying its site needed fixing.

A test for (1) and (2) has to assert a *negative* -- that a compliment never
reaches the drafter as a defect -- because the failure mode is a plausible
string in the wrong place, not an exception.

WHAT IS TESTED HOW, because the two halves are not equally strong:

* `llm._parse_score` and `llm.build_draft_prompt` are exercised for real, with
  the actual scorer-shaped payloads.
* The `lead_engine` writer is guarded by reading its source, the same technique
  `test_stop_reason_vocabulary.py` uses on the templates. Driving `run_campaign`
  for real needs DataForSEO and LLM stubs and would test the stubs. A source
  guard cannot prove the row is right; it can prove the mislabelling was not
  reintroduced, which is the regression that actually happened.
"""

import json
import os
import re

import pytest

import llm

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# A scorer reply for a lead that is doing everything right. Every entry in
# `reasons` is a compliment, which is the point: this is what the evidence
# legitimately looks like for a strong lead, and it is exactly the shape that
# used to land in `weakness`.
PRAISE = [
    "Strong digital presence (presence=100)",
    "Contact info found (7 phones, has_phone=true, contactability=50)",
    "Multiple social profiles matched the website",
]
GAP = ["No email address published (6 phones and 3 socials found, but no mailto)"]


def _engine_source():
    """lead_engine.py with full-line comments removed.

    The comments matter here: the fix documents the OLD line verbatim, so a
    naive scan of the raw source finds `lead["emails_extra"] =
    intelligence.get("phones_found", [])` in the explanation of why that line
    is gone and reports the bug as still present. It did, on the first run of
    this file. Blank out comments rather than dropping the lines, so line
    numbers in a failure still point at the right place.
    """
    path = os.path.join(APP_DIR, "lead_engine.py")
    with open(path, encoding="utf-8") as fh:
        return "\n".join("" if line.lstrip().startswith("#") else line
                         for line in fh.read().splitlines())


# --------------------------------------------------------------------------- #
# The scorer returns evidence and gaps as separate things
# --------------------------------------------------------------------------- #
def test_the_scorer_can_return_a_gap_separate_from_its_evidence():
    out = llm._parse_score(json.dumps({"opportunity_score": 80,
                                       "confidence_score": 65,
                                       "reasons": PRAISE, "gaps": GAP}))
    assert out["reasons"] == PRAISE
    assert out["gaps"] == GAP


def test_a_scorer_that_returns_no_gaps_yields_an_empty_list():
    # NOT a missing key and not None. A provider that predates `gaps`, or a
    # model that ignores the key, must degrade to "no gap flagged" -- which the
    # drafter has an honest fallback for -- rather than to a crash or to the
    # evidence leaking through as a substitute.
    # A bare string is deliberately NOT in this list: `_str_list` coerces it to
    # a one-item list, which is the right reading of a model that ignored
    # "array". It is covered in the coercion test below.
    for payload in ({}, {"gaps": None}, {"gaps": []}, {"gaps": {"nope": 1}}):
        body = {"opportunity_score": 10, "confidence_score": 10,
                "reasons": PRAISE, **payload}
        out = llm._parse_score(json.dumps(body))
        assert out["gaps"] == [], payload
        assert out["reasons"] == PRAISE, payload


def test_gaps_are_coerced_and_stripped_like_reasons():
    out = llm._parse_score(json.dumps({"gaps": ["  a gap  ", "", None, 7],
                                       "reasons": "a single reason"}))
    assert out["gaps"] == ["a gap", "7"]
    assert out["reasons"] == ["a single reason"]


def test_a_null_slot_does_not_become_the_string_none():
    """`None` is dropped, not stringified.

    `str(None)` is `"None"`, which is truthy, so the obvious implementation
    keeps it -- and a model emitting `["a real gap", null]` to leave a slot
    empty would put the literal word "None" into the draft prompt under "WHY
    THEY MAY NEED THIS". Both fields share one helper so this is fixed for the
    scorer's evidence as well, where it had the same wart.
    """
    out = llm._parse_score(json.dumps({"reasons": ["ok", None],
                                       "gaps": [None]}))
    assert out["gaps"] == []
    assert out["reasons"] == ["ok"]
    for line in out["reasons"] + out["gaps"]:
        assert line.lower() != "none"


def test_the_scorer_is_told_the_two_are_different():
    """The prompt has to carry the distinction, not just the parser.

    A model asked only for `reasons` has no way to know a compliment is not a
    gap, so the schema of required keys is the part that actually makes the
    field exist. Asserting `"gaps" in llm._SYSTEM` would NOT check that: the
    explanatory prose below the schema also contains the word, so dropping the
    key from the schema while leaving the prose would pass -- and the model
    would keep returning the keys it was actually asked for. Hence the slice.
    """
    system = llm._SYSTEM
    keys = system.split("with exactly these keys:")[1].split("first_line")[0]
    assert "gaps" in keys, "gaps is not in the scorer's required-key schema"
    assert "reasons" in keys and "opportunity_score" in keys, (
        "the slice is not the schema -- this test would check nothing")
    assert "reasons and gaps are DIFFERENT" in system
    # And it must be told what to do when there is no honest gap, or inventing
    # one is the only way to fill the field.
    assert "empty array" in system


# --------------------------------------------------------------------------- #
# Praise never reaches the drafter as a defect
# --------------------------------------------------------------------------- #
def test_praise_never_reaches_the_draft_under_why_they_may_need_this():
    """The regression, asserted directly.

    Scored evidence is fed in as `reasons` only. The drafter must not receive
    any of it as a gap -- that is the whole bug.
    """
    out = llm._parse_score(json.dumps({"opportunity_score": 80,
                                       "confidence_score": 65,
                                       "reasons": PRAISE, "gaps": []}))
    assert out["gaps"] == []

    # `weakness` is what `build_draft_prompt` receives, and the engine now
    # joins it from `gaps`. With no gaps that is the empty string, which is
    # what the prompt's own fallback exists for.
    prompt = llm.build_draft_prompt(
        business={"business_name": "Acme Dental"}, intelligence={},
        niche="web design", weakness="; ".join(out["gaps"]))

    for line in PRAISE:
        assert line not in prompt.split("WHY THEY MAY NEED THIS")[1], line


def test_the_same_fact_can_be_a_gap_and_the_prompt_accepts_it():
    # The positive case: when the scorer DOES name a lack, it reaches the
    # drafter, and it reaches it under the heading that asks for one.
    out = llm._parse_score(json.dumps({"reasons": PRAISE, "gaps": GAP}))
    prompt = llm.build_draft_prompt(
        business={"business_name": "Acme Dental"}, intelligence={},
        niche="email infrastructure", weakness="; ".join(out["gaps"]))

    assert GAP[0] in prompt
    assert "WHY THEY MAY NEED THIS" in prompt
    # And none of the praise came with it.
    for line in PRAISE:
        assert line not in prompt


def test_the_prompt_names_what_the_field_should_hold():
    prompt = llm.build_draft_prompt(business={}, intelligence={}, niche="x",
                                     weakness="no email address published")
    heading = prompt.split("WHY THEY MAY NEED THIS")[1].split("\n")[0]
    assert "LACK" in heading
    assert "never around a strength" in heading


def test_the_prompt_warns_against_strengths_only_when_a_gap_was_flagged():
    """Legacy rows are why this caution exists, and why it is conditional.

    A row scored before `gaps` existed still has the old reason list sitting in
    `weakness`, and rows are only re-scored on a new run -- so the legacy shape
    stays reachable and the prompt has to survive it. It is not appended to the
    no-gap fallback, which already says the right thing and would only get
    noisier for it.
    """
    with_gap = llm.build_draft_prompt(business={}, intelligence={}, niche="x",
                                      weakness="Strong digital presence (presence=100)")
    assert "Do NOT treat a strength as a defect" in with_gap

    without = llm.build_draft_prompt(business={}, intelligence={}, niche="x",
                                     weakness="")
    assert "Do NOT treat a strength as a defect" not in without
    assert "no specific gap flagged" in without


# --------------------------------------------------------------------------- #
# The engine writes the right values into the right columns
# --------------------------------------------------------------------------- #
def test_weakness_is_built_from_gaps_not_from_evidence():
    src = _engine_source()
    match = re.search(r'lead\["weakness"\]\s*=\s*"; "\s*\.join\((\w+)\)', src)
    assert match, 'lead["weakness"] = "; ".join(?) not found in lead_engine'
    joined = match.group(1)
    assert joined != "reasons", (
        "weakness is joined from the scorer's EVIDENCE again -- that is the "
        "bug that made a STRONG lead's email say its site needed fixing")
    # And that name must be fed from the scorer's `gaps` key, not from
    # something locally invented.
    feed = re.search(rf'{joined}\s*=\s*judged\.get\("([^"]+)"', src)
    assert feed, f"{joined} is not read from `judged` in lead_engine"
    assert feed.group(1) == "gaps"


def test_the_gaps_key_the_engine_reads_is_the_one_the_parser_returns():
    """Ties the two halves together across a rename on either side.

    The engine's source guard and the parser's behaviour are tested separately
    above; both would still pass if `llm._parse_score` renamed its output key,
    because nothing else in the repo names it. This is that link.
    """
    src = _engine_source()
    match = re.search(r'gaps\s*=\s*judged\.get\("([^"]+)"', src)
    assert match
    key = match.group(1)
    out = llm._parse_score(json.dumps({key: ["a real gap"]}))
    assert out.get("gaps") == ["a real gap"], (
        f"lead_engine reads `{key}` off the scorer, but llm._parse_score does "
        f"not return that key -- the gap would silently vanish")


def test_the_evidence_is_kept_not_discarded():
    # Re-homed, not deleted: this is the only record of WHY the scores landed
    # where they did, and dropping it to stop it being misread would trade one
    # bug for another.
    src = _engine_source()
    assert re.search(r'breakdown\["model_reasons"\]\s*=\s*reasons', src), \
        "the scorer's evidence is no longer persisted anywhere"


def test_phones_are_not_written_into_an_email_column():
    src = _engine_source()
    for match in re.finditer(r'lead\["emails_extra"\]\s*=\s*(.+)', src):
        value = match.group(1).strip()
        assert "phones_found" not in value, (
            "emails_extra holds phone numbers again -- anything iterating it "
            "for an address gets '(512) 661-7896'")
        assert value == "[]", value
