---
name: MahaVISTAAR
type: pathway
schema_version: 1
sector: Agriculture
geography: Maharashtra, India
population_served: Smallholder and marginal farmers, particularly women farmers
stage_reached: Scale
contributing_org: Department of Agriculture, Government of Maharashtra
key_dates: 2023-2025
summary: A generative AI conversational agricultural advisory system delivering personalised, pest, weather, and market advisories in conversational Marathi.
scale_impact: Over 1 million farmer interactions, 48-hour to 6-hour data refresh cycle achieved.
cost_anchor: Setup ~₹50L, run-rate ~₹2-3 per conversational query at scale.
build_effort: 9 months initial build to pilot, core team of 8 engineers and domain experts.
downstream_adoptions:
  - Ethiopia ATI (Agricultural Transformation Institute)
---

# MahaVISTAAR

## Section 0: Reading Guide
MahaVISTAAR is an exemplar pathway in the Agriculture sector reaching **Scale**.
Its primary architectural contribution is the clean decoupling of the LLM reasoning layer
from state-level data registries via an API gateway.

## Section 1: Pathway Identity
- **Sector:** Agriculture
- **Geography:** Maharashtra, India
- **Target Population:** Marginal farmers seeking real-time crop advisory in Marathi.

## Section 2: 4×4 Coverage Grid

| Dimension | Explore | Define | Pilot | Scale |
|---|---|---|---|---|
| **Persona** | ●●● | ●●○ | ●●○ | ●○○ |
| **Solution** | ●●○ | ●●● | ●●● | ●●○ |
| **Institution** | ●●○ | ●●● | ●●○ | ●●● |
| **Ecosystem** | ●○○ | ●●○ | ●●○ | ●●○ |

*(Dense knowledge exists in Solution and Institution across Define, Pilot, and Scale).*
