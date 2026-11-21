<p align="center">
  <img src="assets/branding/medautopractice-logo.png" alt="Med Auto Practice logo" width="132" />
</p>

<p align="center"><strong>English</strong> | <a href="./README.md">中文</a></p>

<h1 align="center">Med Auto Practice</h1>

Med Auto Practice (MAP) is an open-source agent for clinicians. It brings chart entries, lab results and nursing notes together for one encounter, helps draft progress notes with traceable sources, and brings proposed order changes back to you for review.

Start with records you already have, or ask MAP to help connect your hospital system. It checks which sources are available for the task and helps you verify the missing step. The next time you need the same information, MAP can reuse the connection your site has verified.

<p align="center">
  <img src="assets/branding/medautopractice-overview.png" alt="The MAP workflow from a clinician's request through record collection, drafting and review to entry in the hospital system and verification of the result" width="100%" />
</p>

## Start with a clinical task

> “Turn these records into a timeline. Show where each diagnosis, medication and lab result came from, and flag anything that conflicts.”

> “I want to use our hospital's lab results in inpatient progress notes. We have an integration platform. Help me retrieve one result from the test system and check the encounter, unit and reporting time.”

> “Pick up the orders I was reviewing. Show me the proposed changes and the evidence for each one.”

For records you supply, MAP presents structured findings and questions first, with the original text available for review. For hospital data, it looks first for an authorized native interface or integration middleware. It can also use a browser or desktop through Computer Use, or IP KVM for an isolated workstation. In the order workflow, the clinician reviews the exact proposal and checks the result read back from the hospital system after submission.

## Use and develop MAP

Install **Med Auto Practice** from the OPL Software Center or add the plugin directly to Codex. Start a conversation with the task you want to complete. The public package includes a synthetic case, a sample deployment profile and a working FHIR lab reader. Each hospital enables clinical workflows according to its own authorization and validation. [Getting started](docs/getting-started.md) · [Hospital integration examples](docs/hospital-adapters.md)

Developers can connect hospital middleware, a desktop host or a clinical proposal service while reusing MAP's patient and encounter checks, clinician review and independent readback. The engineering guides are in Chinese. [Adapter development](docs/adapter-development.md) · [Architecture](docs/architecture.md) · [Product scope](docs/product.md) · [Contributing](CONTRIBUTING.md) · [Apache-2.0 license](LICENSE)
