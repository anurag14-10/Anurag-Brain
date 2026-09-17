---
name: Data Layer Separation via API Gateway
type: unit
schema_version: 1
pathway: mahavistaar
dimension: Solution
sub_category: Model, Architecture, and Infrastructure
stage: Pilot
unit_type: Failure and Fix
applies_when: Multiple external data sources with different owners and update cadences; government deployment where data accountability must remain with named departments.
fails_when: Single, stable, internally-owned data source with no requirement for departmental accountability separation.
before: Data errors and database schema changes required rebuilding and redeploying the bot prompt and model architecture.
after: Data errors and schema changes are fixed by the accountable data owner at the API gateway without touching the AI layer.
also_relevant_at:
  - Define
  - Scale
toolkits:
  - api-gateway-data-layer-pattern
---

# Data Layer Separation via API Gateway

### Failure
Direct hardwiring of the AI layer to the ICAR and APMC mandi databases caused repeated system failures. A schema change or temporary network outage in an upstream government database required a redeploy of the AI orchestrator.

### Fix
Introduced a standardised API gateway between the conversational AI layer and the institutional data sources. The AI retrieves real-time context via structured API endpoints but never queries underlying database tables directly.

### Insight
At scale with multiple institutional data sources, data-layer separation is the structural difference between a fragile prototype and a maintainable public service.
