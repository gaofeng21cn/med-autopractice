<p align="center">
  <img src="assets/branding/medautopractice-logo.png" alt="Med Auto Practice logo" width="132" />
</p>

<p align="center">
  <a href="./README.md">中文</a> | <a href="./README.en.md"><strong>English</strong></a>
</p>

# Med Auto Practice

**From clinical intent to verifiable clinical workflows.**

Med Auto Practice (MAP) is an agent for clinician-directed work across outpatient and inpatient care. Its planned scope connects patient records, draft orders and notes, clinician review, authorized system entry, and verification of the resulting state. The first planned execution path uses IP KVM with Windows. Its Codex primary Skill is the intended entrypoint. MAP can be installed and called independently while following the OPL Agent Apps shared-foundation principles: reusable knowledge uses Framework source APIs, and device connections and credentials remain with their existing provider. The [architecture](docs/architecture.md#agent-共用基座) defines the shared capabilities and clinical-data boundaries.

> **In development:** This repository contains separate medical-proposal routing, generic dependency plans for all nine phase-one workflows, and a synthetic-data task engine. Deployments use replaceable IP KVM and hospital-system adapters. Site names, vendors, versions, UI paths, and qualification evidence are configured by each deployment outside this source repository. No clinical workflow is callable.

<p align="center">
  <img src="assets/branding/medautopractice-overview.png" alt="Med Auto Practice phase-one plan: clinical tasks, patient records, draft notes and orders, clinician review, system entry, and result verification; supported by patient and encounter binding, source and time, and access and review controls." width="100%" />
</p>

## What MAP Is Designed To Do

Clinical work involves judgment and a steady flow of record gathering, drafting, review, and follow-up. Relevant facts often sit across systems; a proposed change and its completed system state are easy to confuse. MAP is being designed to keep the patient's record, the clinician's intent, drafts, review decisions, and verified outcomes connected within one task.

After integration, a clinician could ask MAP to:

- Assemble a sourced timeline from prior outpatient and inpatient records, identifying missing or conflicting information.
- Prepare a progress-note draft from documented events and today's completed round, highlighting facts that still need confirmation.
- Draft admission orders against a clinician-confirmed plan and flag medication, dose, route, and investigation details for review.
- Check a discharge record for omissions, review findings, signature state, and approaching deadlines.

Tasks are intended to continue from verified facts and recorded progress. A change in patient, encounter, or fact version requires another check before work proceeds.

## Planned Capabilities

| Area | Phase-one scope |
| --- | --- |
| Patient history | Gather and structure outpatient and inpatient records, with source, time, completeness, and conflicts preserved. |
| Order sets and patient orders | Prepare and review outpatient and inpatient order sets, admission orders, changes informed by results and nursing records, and discharge medication drafts. |
| Clinical documentation | Draft inpatient and structured outpatient notes from actual clinical events, following current institutional rules without inventing missing facts. |
| Discharge review | Identify incomplete or conflicting records, review and signature status, proposed corrections, and time-sensitive follow-up. |

Disease-specific cohorts, data organization for research, and longitudinal follow-up are planned for a later phase. Phase one is intended to cover the full scope above; a single demonstration would not establish that the clinical workflow is complete.

## Clinical Responsibility

MAP would organize evidence, drafts, proposed changes, and task outcomes. Clinicians retain clinical judgment, approval of treatment plans, and formal signatures. The hospital system remains authoritative for the actual medical record, orders, and execution state. An order entered is not proof that treatment occurred, and a clicked save button is not proof that a document was saved; completion requires a readback from the target system.

Real deployment requires institution-approved access and patient-data handling. No hospital clinical validation has been completed. Planned PACS navigation does not imply automated image interpretation from remote-control screens.

## The Med Auto Family

| Agent | Focus |
| --- | --- |
| **Med Auto Science · MAS** | Medical research, analysis, evidence, and manuscripts. |
| **Med Auto Grant · MAG** | Medical grant planning, writing, review, and application materials. |
| **Med Auto Cast · MAC** | Medical education video planning, production, review, and delivery. |
| **Med Auto Practice · MAP** | Clinical records, order collaboration, documentation, and discharge quality checks. |

MAP owns clinical task meaning and workflow state. The separate `glkvm-native` project owns authorized device connections, observation, keyboard and pointer control, waits, and human takeover. MAP does not duplicate that device layer.

Within MAP, `medicine/` produces evidence-linked candidate proposals and can select a different provider or model per capability. `workflows/` defines required source coverage and clinician review gates; the task engine manages authorization, execution, readback, and acceptance. Neither a model proposal nor a visible vendor-client screen establishes a completed hospital action. The [architecture document](docs/architecture.md) is the source of truth for module and state ownership.

## Current Status

Version **`0.0.2`** remains a development scaffold. The medical-proposal router and local task engine exercise audit, visible-content readback, and clinician acceptance with synthetic inpatient note and order cases. The thin direct-consumer adapter uses OPL Framework knowledge source APIs and preserves exact `knowledge_refs` when a proposal is revised. This repository provides generic contracts, synthetic validation, and a reference implementation. A synthetic-only SQLite store verifies restart recovery and uncertain-submission handling; it is not approved for patient data. The capability catalog marks every clinical capability as `planned` and `callable=false`. Deployment-specific adapters, target-system readback, and clinical acceptance remain outside this source repository and require separate qualification.

## Software identity and public source

The catalog uses **Med Auto Practice** as the display name and `med-autopractice` as its stable Package and plugin ID, at version **0.0.2**. This repository provides public source, generic contracts, synthetic data, and read-only inspection commands. `opl-package.json` retains the software identity and content lock in `development_only` source mode. The local verification commands run from a source checkout. Deployment teams may reuse the source in a controlled carrier, while site profiles, patient data, and evidence remain outside this repository. See [public scope and deployment boundary](docs/public-scope.md) and [software management and clinical qualification](docs/architecture.md#软件管理与临床资格).

`python3 scripts/project_status.py --json` works from the source tree or a materialized plugin root and reports package integrity and clinical qualification separately. Software inspection does not change `planned`, `callable=false` clinical capabilities. Removing the plugin preserves external deployment profiles, patient records, and task stores.

The local status command checks project metadata only. It does not connect to a device or hospital system:

```sh
git clone https://github.com/gaofeng21cn/med-autopractice-reference.git
cd med-autopractice-reference
python3 scripts/project_status.py --json
python3 scripts/site_plan.py revise_orders
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The site-plan command reads a generic contract or an explicitly selected local profile; tests use synthetic records. For the detailed plan, see [product scope](docs/product.md), [architecture](docs/architecture.md), [workflows](docs/workflows.md), [deployment profiles](docs/site-integration.md), [agent interface](docs/agent-contract.md), [implementation roadmap](docs/roadmap.md), [research evidence](docs/research.md), and the [capability catalog](contracts/capabilities.json). These technical references are currently maintained in Chinese.

## License

The source and documentation are licensed under Apache License 2.0. See [NOTICE](NOTICE) for branding and provenance, [CONTRIBUTING](CONTRIBUTING.md) to contribute, and [SECURITY](SECURITY.md) for private reports.
