# The Ledger

> **EXPERIMENTAL SOFTWARE — FOR EVALUATION BY SCHOOL AND DISTRICT IT PROFESSIONALS ONLY.**
> Not a finished product. Not certified, audited, or approved for use with student data. Not intended for use by individual teachers. Provided "as is", with no warranty and no liability on the part of the author or Paragon Electrical Contracting, LLC (d/b/a Paragon, LLC and ParagonK12, LLC). **Read [DISCLAIMER.md](DISCLAIMER.md) before going any further.**

The Ledger is an experimental reference implementation of an AI-assisted system for scoring handwritten work. Pages are scanned to PDF; the software reads each page, sends images to a cloud AI provider, and produces a feedback PDF per page set plus a CSV of scores.

This repository is published so that **IT, data-privacy, and instructional-technology staff** at schools and districts can study the design, build it in a test environment, and decide whether it merits further development under their own institution's policies. Nothing here is a recommendation to deploy it.

The original appliance was built and tested in October 2026 on Debian 13.7 running on a Lenovo ThinkCentre M700 Tiny. *Patent pending.*

---

## Before you evaluate

- **Get approval first.** Do not install or run this software on any network, or with any real student work, without the approvals your district or institution requires.
- **Use test material.** Evaluate with blank or synthetic documents, and have a licensed educator review every score before it is used. See [SECURITY.md](SECURITY.md).

## Install Ledger

All three builds are available from this repository. Clone it or [download the source ZIP](https://github.com/everettstuckey/ledger-grading-appliance/archive/refs/heads/main.zip), then follow the guide for your operating system.

| Operating system | Setup guide | Launcher or installer |
|---|---|---|
| Windows | [Windows desktop setup](docs/WINDOWS.md) | `Start-Ledger.ps1` |
| macOS | [macOS desktop setup](docs/MACOS.md) | `Start-Ledger.command` |
| Linux | [Debian 13 appliance setup](docs/INSTALL.md) | `build-ledger.sh` |

The Linux installer targets Debian 13 with the Xfce desktop on a dedicated test machine. Use a scanner that produces PDFs. The [hardware guide](docs/HARDWARE.md) records the original Linux test system.

## How it works

1. A document is laid out in the Ledger format: a title, an ID line, a page number, and tagged answer boxes. The rules are in **[docs/ASSIGNMENT_FORMAT.md](docs/ASSIGNMENT_FORMAT.md)**; blank examples are in **[samples/](samples/)**.
2. The paper documents are scanned to PDF and placed in the **02 Inbox Scans** folder.
3. The software reads each page's title and ID, prepares images (see the table below), and sends them to the AI provider together with a blank copy of the document and the scoring instructions entered by the operator.
4. The software writes a *conspectus* PDF per ID — the scanned pages, item-by-item feedback, and a score — plus a `Tabula.csv` of scores. A gradebook view can export a CSV.

Ledger supports **Google** (Gemini through AI Studio or Vertex), **Anthropic** (Claude), **Azure OpenAI** (GPT-6.1 Sol or GPT-6 Astra through Microsoft Entra ID), the **Codex desktop app**, and a **Microsoft Copilot desktop bridge** for evaluation. [Azure OpenAI setup](docs/AZURE_OPENAI.md) · [Codex desktop setup](docs/CODEX_DESKTOP.md) · [Copilot desktop setup](docs/COPILOT_DESKTOP.md).

## What leaves the machine

Every image below is sent over the internet to the AI provider configured on the machine. **Review this table against your institution's data-governance policy before testing with any real material.**

| Sent to the provider | When | What it can contain |
|---|---|---|
| Every page of the blank master document | Once per batch | The assignment itself |
| A crop of the title area | Once per batch, taken from the **first scanned page** | Whatever is written in that area |
| A crop of the ID box | Every scanned page | The ID written on the page (intended to be a pseudonym the operator assigns — see below) |
| Crops of the boxed answer regions | Every page, **WORKSHEET (BOXED)** mode | Handwriting inside the boxes, and anything else inside them |
| **Whole pages**, downscaled | Every page, **ESSAY (FULL-PAGE)** mode | **Everything on the page**, including any name or other writing outside the boxes |

The software does not send a roster, real names it has stored, or the gradebook. It cannot stop a person from writing a name, or anything else, on the paper, and in ESSAY mode the whole page is sent.

The ID box is intended to hold a pseudonym assigned by the operator. The roster screen warns against using real names or district-issued ID numbers as the identifier; the software cannot enforce that.

## Your AI provider

What a provider does with images after they leave the machine depends on its terms. Those terms differ by account type and change over time. Have your institution review the terms before configuring a provider.

When a Google AI Studio key is entered, the setup screen requires the operator to confirm that billing is enabled on the key's project and links to Google's terms. All provider charges appear on the account whose key is entered.

## Scoring instructions

The software ships with **no built-in scoring instructions**; the operator enters them. The **[prompts/](prompts/)** folder holds six example instruction sets (Math, Chemistry, AP Chemistry, College Physics, and two for Social Studies). They are examples, not validated rubrics.

## Status

- **v1.0.x — experimental evaluation release.** Built from a blank disk and exercised against both providers on 2026-10-24. Intended for evaluation by a technically qualified operator. See [SECURITY.md](SECURITY.md) and [DISCLAIMER.md](DISCLAIMER.md).
- A separate, hardened build exists for controlled pilots. It is not part of this release.

## The web app

A separate browser-based version of The Ledger is in development under its own terms. It is not covered by this repository. Inquiries: **ledgermachine1@gmail.com**.

## License and disclaimer

This software is released under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE). The license, including its disclaimer of warranty and limitation of liability, governs your use, together with the additional terms in [DISCLAIMER.md](DISCLAIMER.md).

**No warranty. No liability.** The author and Paragon Electrical Contracting, LLC (d/b/a Paragon, LLC and ParagonK12, LLC) make no representation that this software is accurate, secure, lawful to use in your jurisdiction, or suitable for any purpose, and accept no liability for any consequence of building, installing, or running it.

## About the author

The Ledger was written independently by a Missouri high-school chemistry teacher, on personal time and personal equipment, outside his employment, and is not affiliated with or endorsed by any school district. See [DISCLAIMER.md](DISCLAIMER.md), section 6.

## Contributing

Bug reports and evaluation questions are welcome in Issues and Discussions. Pull requests are not accepted at this time — see [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: see [SECURITY.md](SECURITY.md).
