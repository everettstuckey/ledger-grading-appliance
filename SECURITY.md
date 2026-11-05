# Security

> Experimental software for evaluation by school and district IT professionals; see [DISCLAIMER.md](DISCLAIMER.md).

## This release is not hardened

It has not been security-audited or penetration-tested; this page describes the design as published.

A machine built from this repository is an **ordinary Debian desktop with one extra program**. It has:

- **no full-disk encryption** — anyone with the machine or its drive can read everything on it, including scans, scores, the roster, and provider keys;
- **no hardware-key login, no kiosk mode, and no USB restrictions**;
- no protection beyond the Debian account password and whatever your institution adds.

Do not place real student work on it unless your institution has evaluated these limits and approved it. Securing the machine, the network it sits on, and the room it sits in is the operator's responsibility.

## How the software handles sensitive material, as designed

These statements describe intended behavior. Verify them in your own evaluation before relying on them.

- **API keys** are written by the setup wizard to `/etc/paragon/.env`, owned by root and readable by root and the `paragon` account. The software is designed not to write keys to its logs and to send each key only to the provider it belongs to. Because the disk is not encrypted, the key is readable by anyone with physical access to the drive.
- **Azure OpenAI** uses the `paragon` account's Azure CLI sign-in. Azure CLI maintains its own local credential cache. When retiring an appliance, sign out of Azure CLI as `paragon` in addition to running Ledger's factory reset.
- **Images sent to the provider.** See the table "What leaves the machine" in the [README](README.md). In short: the blank master pages, a title crop, an ID crop from every page, and either cropped answer boxes (WORKSHEET mode) or **whole pages** (ESSAY mode).
- **Stored on the machine.** Scans, conspectus PDFs, `Tabula.csv` files, the roster, scores, and settings are stored unencrypted in the operator's home folder. A factory reset (ADVANCED menu) removes the provider keys and stops the AI service; it does **not** delete these files.
- **The AI service** (`ledger-daemon`) listens on `127.0.0.1:5000` only and is not designed to accept connections from the network.
- **The build script** installs packages, creates `/opt/ledger`, and installs the service. It does not open ports, add firewall rules, or grant passwordless `sudo`.

## Reporting a security problem

Email **ledgermachine1@gmail.com** with "Ledger security" in the subject. Please do not open a public issue for it. Reports are read on a best-effort basis; there is no guaranteed response time and no commitment to issue a fix.
