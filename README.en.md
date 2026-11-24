<p align="center">
  <img src="assets/branding/medautopractice-logo.png" alt="Med Auto Practice logo" width="132" />
</p>

<p align="center"><strong>English</strong> | <a href="./README.md">中文</a></p>

<h1 align="center">Med Auto Practice</h1>

Med Auto Practice (MAP) is an open-source agent for everyday clinical work. The idea is that after rounds, you can hand off progress notes and order entry to MAP the way you would hand off a task to a colleague.

MAP is designed to work from the goal you give it and use your hospital’s existing systems. It finds the relevant records, carries out the work you approve, and reports what the system saved.

<p align="center">
  <img src="assets/branding/medautopractice-overview.png" alt="The MAP workflow from a clinician's request through record collection, drafting and review to entry in the hospital system and verification of the result" width="100%" />
</p>

## How the work continues after rounds

After rounds, hand MAP your observations, assessment, and plan for the day. It works within the same encounter and uses your hospital’s existing environment to find relevant labs, nursing notes, and prior records. It moves ahead with the information available and brings you the changes or missing details that need your judgment.

When reviewing a progress note, you can check when the key findings were recorded and where they came from. When reviewing orders, you see the proposed additions, changes, and discontinuations with their rationale. Once you confirm the changes, MAP enters them and reopens the original system to check what was saved. You make the clinical decisions and sign the records.

The work can span more than one session. When you return to a batch of orders, MAP brings back the draft, the evidence, and the current progress from the same task. It checks any new results that affect the proposal. If entry was interrupted, it checks what the original system saved before continuing.

## Give MAP a task

Inpatient notes and order changes are the first hospital integration priorities. These examples show the intended workflow.

> “Write today's progress note using my notes from rounds, last night's nursing notes, and the latest results. Let me review it first, then save the approved note in the chart.”

> “Prepare the orders for the treatment plan we just agreed on. Show me what you'd add, change, or discontinue. Once I confirm, enter the orders and check what the system saved.”

Disease cohorts and follow-up are planned around the same way of working.

> “Update this disease cohort using our department's inclusion criteria. Add each patient's latest results and show me who's due for follow-up and what still needs checking.”

## Get started

Install **Med Auto Practice** from the OPL Software Center, or add it directly to Codex. In the public preview, start with records you already have. Ask MAP for a timeline with sources, inconsistencies, and missing information. [Installation guide](docs/getting-started.md) · [Development progress](docs/roadmap.md) · [Documentation index](docs/README.md)

[Hospital setup & development](docs/hospital-adapters.md) · [Contributing](CONTRIBUTING.md) · [Apache-2.0 license](LICENSE)
