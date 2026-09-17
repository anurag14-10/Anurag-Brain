# Sources — Anurag-Brain

Every source system or input medium the brain consults when capturing and corroborating
pathways, units, and toolkits.

## Rules that apply to every source

- **Read-only unless stated otherwise.**
- **Say which sources were used.** Silence about a source reads as corroboration.
- **Human judgment is mandatory on condition tags.** Never automate `applies_when` and `fails_when`.

---

## Registry

### `transcript`

| | |
|---|---|
| **What** | Semi-structured or unstructured practitioner/adopter interview transcripts |
| **Access** | `read` |
| **Cost** | moderate |
| **Probe** | Contains speaker turns, deployment reflections, and direct quotes |

**Trust for:** Failure narratives, actual workarounds, internal stakeholder conflicts, unvarnished fixes.
**Do not trust for:** Final exact budget figures or contract dates (corroborate with documents).

---

### `document`

| | |
|---|---|
| **What** | Formal project reports, architecture blueprints, RFPs, evaluations, presentations |
| **Access** | `read` |
| **Cost** | cheap |
| **Probe** | Structured document files (PDF/Markdown/Docx) |

**Trust for:** Formal architecture diagrams, partner names, SLA specifications, headline reach numbers.
**Do not trust for:** Honest failure modes and workforce friction (formal docs often sanitise failure).

---

### `conversation`

| | |
|---|---|
| **What** | Live interactive session with an adopter, contributor, or engineer |
| **Access** | `read` |
| **Cost** | cheap |
| **Probe** | Current active conversational context |

**Trust for:** Adopter intent, stage clarification, immediate follow-up on condition tags.
**Do not trust for:** Verifiable past deployment dates without source check.

---

### `public`

| | |
|---|---|
| **What** | Public whitepapers, press releases, news reports, conference papers |
| **Access** | `read` |
| **Cost** | cheap |
| **Probe** | Publicly accessible URL or published PDF |

**Trust for:** Public milestones, published awards, official public announcements.
**Do not trust for:** Internal trade-offs, architecture failures, proprietary cost structures.

---

## Fields no source can fill automatically

Condition tags require synthesis and understanding of adopter boundaries:

```yaml
never_automated:
  - units.applies_when
  - units.fails_when
```
