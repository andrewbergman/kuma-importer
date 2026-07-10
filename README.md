<p align="center">  

<img src="./logo.svg" width="500" />

</p>

<h1 align="center">kuma-importer</h1><p align="center">

</p>


<p align="center">
<img src="https://img.shields.io/badge/version-v0.1.0-blue" />
<img src="https://img.shields.io/badge/python-3.10%2B-blue" />
<img src="https://img.shields.io/badge/license-MIT-green" />
</p>


<p align="center">

# kuma-importer

Define, apply, and maintain Uptime Kuma monitors as code using safe, idempotent workflows.

## Demo

<p align="center">
  <img src="demo.gif" alt="kuma-importer demo">
</p>

<p align="center">
  <sub>Animated demo of creating, verifying, and cleaning up monitors</sub>
</p>

<p align="center">
  <a href="demo>

![Version](https://img.shields.io/badge/version-v0.1.0-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

Interactive CLI tool for managing Uptime Kuma monitors using CSV, TXT, and manual workflows.

---

## Why this exists 

Managing Uptime Kuma manually doesn’t scale.
kuma-importer lets you define monitors as code and apply them safely, repeatedly, and predictably.


---

## ⚡ Quickstart (60 seconds)

### 1. Clone repo
bash
git clone https://github.com/andrewbergman/kuma-importer.git
cd kuma-importer


### 2. Create environment
bash
python3 -m venv kuma-env
source kuma-env/bin/activate


### 3. Install dependencies
bash
pip install -r requirements.txt


### 4. Run interactive mode
bash
python kuma_importer.py


---

## Optional CLI usage

Install locally:
bash
pip install -e .


Run:
bash
kuma-import


---

## Versioning

- Current version: v0.1.0

---

## Project Structure


kuma_importer.py
kuma_importer.conf
kuma_defaults.conf
README.md
LICENSE
THIRD_PARTY_NOTICES.md
requirements.txt
pyproject.toml
setup.py
example_monitors.csv
example_domains.txt


---

## requirements.txt

Quick install dependencies:
bash
pip install -r requirements.txt


---

## pyproject.toml

Supports:
- packaging
- CLI install
- dependency pinning

---

## Example CSV

csv
client,name,url,type,interval,maxretries,retryInterval
ExampleClient,example.com,https://example.com,http,60,1,60


---

## Example TXT


example.com
service.example.com


---

## Best Practices

- Always use --dry-run first
- Use profiles for consistency
- Maintain CSV as source of truth

---

## License

MIT License

---

## Acknowledgements

- Uptime Kuma – Louis Lam
- uptime-kuma-api – Lucas Held
- uptime-kuma-api2 community fork

