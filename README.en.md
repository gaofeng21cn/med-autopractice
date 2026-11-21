<p align="center"><img src="assets/branding/medautopractice-logo.png" width="112" alt="Med Auto Practice logo"></p>
<p align="center"><a href="README.md">中文</a> · <strong>English</strong></p>

# Med Auto Practice (MAP)

MAP is an open-source clinical workflow agent for clinicians. It organizes outpatient and inpatient records into structured, source-linked facts, prepares notes and order changes for review, and tracks what the hospital system actually recorded. Clinicians work in Codex. Integration teams connect MAP to the systems their hospital already uses.

## Run a case

With Git and Python 3.10 or later, run the synthetic order demonstration. It uses the Python standard library.

```sh
git clone https://github.com/gaofeng21cn/med-autopractice.git
cd med-autopractice
python3 scripts/map_demo.py
```

Follow one order change through simulated clinician approval, an independent read of the saved object and final acceptance. The output includes the reopened object's `readback.object_ref` and the sequence of `statuses` ending in `accepted`.

## Install the agent

Search for **Med Auto Practice** in the OPL Software Center or install the same package from the CLI.

```sh
opl packages install med-autopractice --json
```

For direct installation in Codex, run the following commands.

```sh
codex plugin marketplace add https://github.com/gaofeng21cn/med-autopractice.git --ref main
codex plugin add med-autopractice@med-autopractice
```

Start a fresh session and ask “Use MAP to run the synthetic order case. Show me the approved version and the result read back from the system.” The walkthrough covers individual approvals and resuming a task. [Getting started](docs/getting-started.md)

## Connect a hospital environment

Start with one operation, such as reading a known lab result for an inpatient note. Prefer an authorized native API or the hospital's integration middleware for structured access. Use direct Computer Use for an available browser or desktop session. IP KVM is an option for a hardware-isolated terminal. A workflow can combine different transports.

| Task | Entry point |
| --- | --- |
| Read a result over a native protocol | Run `python3 examples/synthetic/fhir_read.py`. The local FHIR R4 server returns a quantitative observation with a patient, encounter and resource version. [Hospital adapters](docs/hospital-adapters.md) |
| Plan a pilot | Generate an external profile from the synthetic deployment example, choose a transport per operation and inspect outstanding dependencies and review points. [Deployment walkthrough](docs/site-integration.md#运行合成部署示例) |
| Structure collected records | Export a record and nine fixed-column research tables. The clinician sees structured facts first, then questions and the collected source text. [Fixture and commands](tests/fixtures/README.md) |
| Integrate a model, middleware or desktop host | Implement `ClinicalKnowledge` or `ClinicalSystem` and reuse task identity, approval and readback. [Developer guide](docs/adapter-development.md) |

## Clinical workflows

Phase one covers retrospective records, outpatient and inpatient order sets, admission orders, order adjustments, discharge medication, notes, discharge review and deadlines. Inpatient progress notes and order adjustments are the first site pilot workflows. Disease-specific cohorts and longitudinal follow-up come later. [Product scope](docs/product.md) · [Workflows](docs/workflows.md)

The agent binds facts and source timestamps to a patient and encounter. A clinician approves the specific order proposal. After submission, the adapter independently reopens the target object and checks its contents and state. The clinician can accept or request changes on the same task. An uncertain submission is reconciled against the source system. [Agent contract](docs/agent-contract.md)

## Status and development

The public package includes the Codex skill, resumable synthetic tasks, structured record exports, a FHIR quantity reader and site adapter interfaces. Clinical workflows require site-specific authorization and qualification. Hospitals keep configuration, patient material and evidence in their own controlled storage. Run `python3 scripts/project_status.py --json` to inspect the installed version and capability status. Detailed engineering guides are maintained in Chinese. [Capabilities](contracts/capabilities.json) · [Architecture](docs/architecture.md) · [Roadmap](docs/roadmap.md)

Source and documentation are available under Apache-2.0. Contributions can include workflows, adapters and tests reproducible with synthetic data. [Contributing](CONTRIBUTING.md) · [Public scope](docs/public-scope.md) · [Security](SECURITY.md) · [License](LICENSE)
