# Grounded assessment prompt requirements

> What the **grounded** host model loop's prompt must require of a model-produced assessment.
> Semantic basis: `specs/question-grounded-answer.md` (D1–D8 and the host-guarantees section).
> Enforcement basis: `src/fin/host/grounded_assessment.py`; discipline from `docs/guides/prompt-development.md`.

This document states requirements only; it is not a schema, a template, or a second copy of the contract.

## Name authority

`src/fin/host/grounded_assessment.py` is the single authority for the assessment field names, its
closed vocabularies, and its stable validation error codes; domain words come from `docs/CONTEXT.md`
and evidence kinds from the existing evidence contract that module already binds to. This document
deliberately restates none of them, because a copied field set or vocabulary here is a second authority
that will drift. A prompt for the host loop renders its required JSON shape from the schema itself
rather than carrying hand-copied key names.

## What the model must produce

- **Claim-specific support.** Every material conclusion cites evidence the host admitted for its own
  subject, applicability, source scope, version, and time. Related evidence is not support by
  association; an unsupported assertion is narrowed or withheld.
- **Source statement and derivation kept distinct.** A conclusion the model calculates, synthesizes,
  or infers is labelled as such and names the premises it rests on; a user's confirmed situation is a
  task condition, never evidence for a policy fact.
- **The whole goal assessed.** Every material requested part gets a status; a generated task list is
  not the goal's authority.
- **Partial progress preserved.** Supported portions are delivered while other parts stay explicitly
  unresolved, with the material gap named and any conflict preserved rather than resolved, unless
  evidence establishes which source prevails.
- **A continuation only when useful and permitted.** A further check is proposed for a named gap,
  against a capability the host reported as permitted and currently available, with a reasonable
  prospect of obtaining useful evidence.
- **A clarification only for an actionable condition.** A question is asked only for user-held
  information whose answer would change a feasible next step or conclusion; a source-coverage gap
  alone does not justify one.
- **Failure classes kept distinct.** A lookup that succeeded without support, a permission limit, an
  unsupported condition, a dependency failure, and exhausted recovery are different outcomes, and
  supported findings survive when another part fails.
- **Completion proposed only for a supported whole goal.** Ending the turn, a citation or numeric
  pass, exhausting the tools, or clearing a TODO list cannot promote unresolved work.

## What the host must put in front of the model

These are host-owned facts, and the model is graded against them, so the prompt must carry them as
input: a fact that appears only at validation time grades the model on something it never saw.

- **The goal parts the host committed for this task, by their identities.** Whole-goal coverage is
  computed per committed part identity, so a prompt that never presents the committed list leaves the
  model unable to cover parts it was never told about and guarantees the very under-declaration the
  validator then narrows. Showing the identities is not showing the expected coverage: which parts
  ought to end up supported is grading material and stays out of the prompt.
- **The host's current capability report and its record of operations actually observed.** A proposed
  check is narrowed against what the host reports as permitted and available, and a failure the model
  names survives only if the host observed it; neither question is answerable from the model's side
  unless the prompt states what the host currently reports.
- **Whether any part list was committed at all.** Committing a decomposition is optional, so the
  prompt says plainly when the host committed none. A completion granted in that state is neither
  refused nor read as certified: it is reported as uncertified, because the only part list was the
  candidate's own, and an absent list is not evidence that the goal is uncovered either.

## What the model must never be allowed to assert

Permission, capability availability, whether the basis was committed, which goal parts were committed,
and which operations were actually observed are host facts: the host supplies them as one carrier when
it validates, and the model cannot verify its own evidence, claim a host check passed, or commit task
state, so the prompt asks none of them as answers — it presents them, as the section above requires —
and says plainly that naming a capability does not grant one.
The validator only narrows — it never promotes a claim, a check, or a completion the model did not
already justify. Semantic judgment stays with the model: the validator does not decide support,
applicability, or which conflicting source prevails. Authoritative task state stays with Runtime, which
alone validates against the current basis and applies and persists the outcome.

## Grading material never enters a live prompt

Nothing under `data/grounded_assessment/` — expected outcomes, required or forbidden claims, premise
declarations, or authoring labels that already state a conclusion — may reach the text sent to a model.
Grading material is read only after the model has answered, or in a wiring check that claims nothing
about a model. A prompt holding the answer measures instruction-following, not grounding.

## The one realization today

`scripts/grounded_live_probe_prompt.py` assembles the prompt pair that carries these requirements,
driven by `scripts/grounded_live_probe.py`. Both are developer tooling: neither is an acceptance gate
and neither is the product loop. The product realization is the host wiring — the per-track
instruction assembly in `src/fin/host/agent_instructions.py` and the graph node that would call
`validate_assessment` — which belongs to the host integration owner; the assessment module has no
product caller yet, by design. Changing it is a versioned prompt change under
`docs/guides/prompt-development.md`: run it against the committed examples, and put any discipline a
host check can decide in `src/fin/host/grounded_assessment.py`, not only in the prompt.
