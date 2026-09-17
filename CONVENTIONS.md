# Conventions — Anurag-Brain (AI Diffusion Pathway Wiki)

The schema, rules, and core framework definitions for this wiki.
Written so any AI assistant or human collaborator can capture, retrieve, and synthesize
knowledge from AI deployment pathways without ambiguity.

Controlled-vocabulary fields draw their allowed values from **`MASTERS.md`**.
That file is the single source of truth for them — do not invent values here.

## Scope

This wiki holds structured knowledge generated from AI deployments using the
**AI Diffusion Pathway Framework**. It contains:
1. **Pathways** (`pathways/`): Documented deployments with their identity, effort, context, and 4×4 coverage grid.
2. **Micro-innovations** (`units/`): Tagged, reusable insights, decisions, failure-fixes, and playbooks extracted from deployments.
3. **Toolkits** (`toolkits/`): Reusable technical templates, governance frameworks, prompt patterns, and protocols.
4. **Activities** (`activities/`): Append-only monthly log of what was captured and decided.

**What does NOT belong here:**
- General business CRM or customer contact management.
- Day-to-day task tracking or sprint tickets.
- Raw code repositories or model binaries (link via `external_refs` instead).

## Schema version

Current schema version: **1**

Every entity file carries `schema_version: 1`.

### Version history
- **v1** (2026-09-17): Initial schema for AI Diffusion Pathway Framework.

## Entity types

| Folder | What it holds |
|---|---|
| `pathways/` | A named AI deployment pathway with identity, context, scale, and 4×4 coverage grid |
| `units/` | Micro-innovations — tagged reusable decisions, failure-and-fixes, and playbooks |
| `toolkits/` | Reusable artefacts — templates, prompt patterns, testing protocols, frameworks |
| `activities/` | Append-only monthly log, one file per `YYYY-MM` under `activities/entries/` |

## Slugs

Lowercase, hyphenated, derived from the name (e.g. `mahavistaar`, `mahavistaar-api-gateway-separation`).
Slugs are the join key: frontmatter references other entities by slug.

## Frontmatter Schemas

### 1. Pathway (`pathways/<slug>.md`)
```yaml
---
name: string (required)
type: pathway
schema_version: 1
sector: string (required, vocab: 6)
geography: string (required)
population_served: string (required)
stage_reached: string (required, vocab: 3)
contributing_org: string (required)
key_dates: string
summary: string (required, 2-sentence summary)
scale_impact: string (headline usage and outcome metrics)
cost_anchor: string (setup + run-rate cost order of magnitude)
build_effort: string (time, team size, partner count)
downstream_adoptions: list of strings (known adopters / reuse record)
external_refs: list of URLs/identifiers
---
```

### 2. Unit (`units/<slug>.md`)
```yaml
---
name: string (required)
type: unit
schema_version: 1
pathway: string (required, ref: pathways)
dimension: string (required, vocab: 1)
sub_category: string (required, vocab: 2)
stage: string (required, vocab: 3)
unit_type: string (required, vocab: 4)
applies_when: string (required, condition tag)
fails_when: string (required, condition tag)
before: string (outcome state before decision/fix)
after: string (outcome state after decision/fix)
also_relevant_at: list of strings (vocab: 3, for retrieval only, NOT counted in grid)
toolkits: list of strings (list_ref: toolkits)
external_refs: list of URLs/identifiers
---
```

### 3. Toolkit (`toolkits/<slug>.md`)
```yaml
---
name: string (required)
type: toolkit
schema_version: 1
pathway: string (ref: pathways)
toolkit_type: string (required, vocab: 5)
purpose: string (required)
conditions_for_reuse: string (required)
external_refs: list of URLs/identifiers
---
```

---

## Core Framework Reference

### The 30/70 Thesis
- **Persona + Solution (30%):** Defining and building the right system for the right person.
- **Institution + Ecosystem (70%):** The larger work of enabling adoption, accountability, institutional ownership, and ecosystem support.

### The Four Dimensions
1. **Persona:** Are we solving the right problem for the right person?
2. **Solution:** Are we building the right system to solve it?
3. **Institution:** Can the institution own, absorb, govern, and sustain it?
4. **Ecosystem:** Can the required network of actors execute and support it?

### The Four Adopter Stages
1. **Explore:** Is AI appropriate, and what would it take?
   - *Done when:* Precise excluded-user definition, honest comparison with alternatives, order-of-magnitude cost sense.
2. **Define:** What must be true before building?
   - *Done when:* Named data owners, named mandate holder, architecture posture chosen, safety boundaries designed.
3. **Pilot:** What breaks with real users and real institutional conditions?
   - *Done when:* Failure taxonomy, named institutional response to first public failure, real cost-per-interaction data.
4. **Scale:** Can the institution own, sustain, and continuously improve it?
   - *Done when:* Dedicated budget line, named operational owner, monitoring mechanism, operating model written down.

### The Five Unit Types
1. **Strategic Decision:** A framing, governance, or design decision that shaped what got built. Reusable via its *condition tag*.
2. **Tactical Decision:** A stack, sequence, cost, or implementation decision specific enough to reuse. Reusable via its *before→after*.
3. **Failure and Fix:** Something that broke, the fix, and what the fix revealed about the system. Reusable via the *structural insight revealed*.
4. **Playbook:** A multi-step, gated sequence for a recurring situation ("if X, do Y before Z").
5. **Toolkit Asset:** A reusable technical component, template, prompt pattern, or governance artefact.

---

## The Question Bank

### EXPLORE Stage
- **Persona:** Who specifically is excluded from this service today — and what do they do instead?
  - *Insight form:* Excluded user + named barrier + current workaround.
- **Solution:** What channel or system are you replacing, what does it fail at, and is AI actually the right tool for that failure?
  - *Insight form:* Current channel + failure mode + AI-fit justification.
- **Institution:** Who inside the institution has to personally want this to work — and do they know yet?
  - *Insight form:* Named champion + their specific stake.
- **Ecosystem:** Who else has tried to solve this for this population, and what happened?
  - *Insight form:* Named precedent + what transferred + what didn't.

### DEFINE Stage
- **Persona:** What is the one question a user will ask that this system must answer, or the pilot fails?
  - *Insight form:* Single critical use case + binary success definition.
- **Solution:** Which architecture choices, if wrong, would take six months to undo — and is every data source named, with an accountable owner for each?
  - *Insight form:* Irreversible decisions list + data source registry (source × owner × cadence × accountability).
- **Institution:** Who approves what the system says, have they agreed to own that, and what testing/timeline has the institution committed to before real users?
  - *Insight form:* Content authority + approval process + testing progression.
- **Ecosystem:** Which parts can you not build yourselves — and do you have a named partner for each?
  - *Insight form:* Dependency map: component × build-or-source × named partner.

### PILOT Stage
- **Persona:** Which user interactions are failing — is that a scope problem or a quality problem?
  - *Insight form:* Failure taxonomy: scope vs quality vs experience.
- **Solution:** Which component is causing the most pain, is it replaceable, and which data source is going stale or wrong?
  - *Insight form:* Component failure + replaceability + data quality issue + owner response time.
- **Institution:** What has the institution seen fail publicly — and did they own it or disown it?
  - *Insight form:* First public failure + institutional response (own vs disown).
- **Ecosystem:** Which partner is underperforming — and do you have an alternative?
  - *Insight form:* Partner performance log + contingency plan.

### SCALE Stage
- **Persona:** Are new user segments arriving that the pilot wasn't designed for?
  - *Insight form:* User segment expansion map + design change required per segment.
- **Solution:** Which components are you now unbundling, what triggered it — and which data sources are breaking under scale, do formal SLAs exist?
  - *Insight form:* Unbundling decision (component × trigger × gain) + data SLA map.
- **Institution:** Has the institution absorbed this — budget line, named owner, review cadence — and does the system leave people more capable or more dependent?
  - *Insight form:* Absorption indicators (budget + owner + review cadence) + agency outcome.
- **Ecosystem:** What from your deployment could the next adopter reuse — with what conditions?
  - *Insight form:* Transferable unit + condition tag (applies when / fails when).

---

## Rules for Capturing Units

1. **The Synthesis Test:**
   *Could someone who never saw the raw material make a different decision because of this unit?*
   If yes, it's a unit. If it merely describes what happened, it is not.
2. **Tag every unit:**
   Must include `dimension`, `sub_category`, `stage`, `unit_type`, `applies_when`, `fails_when`.
3. **Stage reflects origin, not potential utility:**
   `stage` reflects where the evidence was discovered (counts in the 4×4 grid).
   `also_relevant_at` reflects where else it is useful (adopter guidance only, never counted in grid).
4. **Write the before→after:**
   Every tactical and strategic unit requires an outcome statement.
5. **Name the failure specifically:**
   "It didn't work" is not a unit. Name the failure, the fix, and the structural insight.
6. **Flag the gaps:**
   Evaluate coverage against **Primary** sub-categories per stage, not raw cell count.
7. **Don't fabricate:**
   If a condition, name, or metric is not in the source, record `"Not documented in the source"`. Never invent details.
8. **Leave unknowns blank.**
9. **One commit per logging event.**

## Activity Log Format

```
- {YYYY-MM-DD}: [{type}] {name} — {one-line summary}
```
Append-only under `activities/entries/YYYY-MM.md`.
