# Contributing

Contributions are welcome. This is a small project with a narrow scope — the goal is to keep it simple, portable, and useful to the amateur radio / ADS-B community.

## Expanding coverage

The easiest way to contribute is to run a sender. Anyone with a local ADS-B receiver (dump1090, adsb.im, or similar) can feed the mesh — no coordination or code changes required. Just point the sender at your receiver and let it announce itself on the network:

```bash
adsb-sender --lat <lat> --lon <lon> --name <your-callsign-or-city>
```

Receivers that have the Chicagoland Reticulum Network configured will pick up your sender automatically. See the README for setup instructions.

## Improving the app

**Bug reports** — open a GitHub issue with your OS, Python version, RNS version, and a clear description of what happened vs. what you expected.

**ADS-B data source support** — the sender accepts any URL returning `{"aircraft":[...]}` or `{"ac":[...]}` JSON. PRs adding documented support for new sources are welcome.

**Landmark data** — `landmarks.csv` currently covers the Midwest. Contributions for other regions (airports, cities, water features) are appreciated.

**Protocol improvements** — changes to `proto.py` that affect the on-wire frame format should bump the version byte and maintain backward compatibility where possible.

## Development setup

```bash
git clone https://github.com/alabbott/adsb-radar
cd adsb-radar
bash setup.sh
```

Dev dependencies (linting only — not required to run the project):

```bash
.venv/bin/pip install ruff
.venv/bin/ruff check src/
.venv/bin/ruff format src/
```

## Pull requests

- One fix or feature per PR — keep the diff focused
- Test sender and receiver end-to-end before submitting
- If you change the binary frame format, update `CHANGELOG.md` and document the new layout in `proto.py`

## Code style

PEP 8, enforced by ruff. Runtime dependencies are limited to `rns` — keep it that way.
