# Anurag-Brain: AI Diffusion Pathway Knowledge Base

This repository is a **Company-Brain Wiki** implementing the **AI Diffusion Pathway Framework**.
It stores structured deployment learnings, micro-innovations, playbooks, and toolkits
designed specifically to accelerate subsequent AI adoptions.

## Repository Structure

```
.
├── pathways/        # Deployments (e.g., MahaVISTAAR, Bhili, Voice AI)
├── units/           # Micro-innovations (tagged reusable units)
├── toolkits/        # Reusable templates, prompt patterns, governance frameworks
├── activities/      # Decision and capture activity logs (by YYYY-MM)
├── scripts/         # Wiki validation, rollup, and integrity tools
├── CONVENTIONS.md   # Brain constitution: rules, schemas, question bank
├── MASTERS.md       # Controlled vocabularies (dimensions, stages, types, sectors)
├── SOURCES.md       # Registered source types and trust levels
├── SOURCE-SEMANTICS.md # Interpretation and field coverage of sources
└── SCHEMA.yml       # Machine-readable entity model and validation rules
```

## Validation

To validate the repository against its schema, references, and vocabularies:

```bash
python3 scripts/validate_wiki.py . --stats
```
