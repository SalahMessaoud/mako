# Contributing to Mako

Thanks for your interest in contributing. This document covers the basics.

## Getting Started

1. Fork the repository
2. Clone your fork locally
3. Install dependencies: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in your RPC URL
5. Run in paper mode: `cd src && python main.py`

## Project Structure

All source code lives in `src/`. See the [Architecture section](README.md#6-architecture) in the README for a full module map.

## Code Style

- Python 3.10+ (type hints, dataclasses, match statements are fine)
- No linter or formatter is enforced -- just keep it consistent with the existing code
- Use `logging` for output, not `print` (except in CLI tools like `monitor.py` and `status.py`)
- Prefer dataclasses over raw dicts for structured data
- Keep functions focused -- one responsibility per function

## Making Changes

1. Create a branch: `git checkout -b feature/your-feature`
2. Make your changes in `src/`
3. Run the tests: `cd tests && python -m unittest`
4. Add a test for any change to trade execution, exits, or strategy logic. The suite is stdlib `unittest`; `tests/helpers.py` provides fakes for the wallet, RPC, PumpPortal and Jupiter, blocks network access and never loads `.env`
5. Test in paper mode before submitting, with a scratch database (`--db /tmp/test.db`)

## Pull Requests

- Keep PRs focused on a single change
- Describe what changed and why in the PR description
- If adding a new strategy, follow the pattern in `strategies.py` (class with `evaluate()` returning `Signal`)
- If adding config options, update both `config.py` (with env var loading) and `.env.example`

## Areas Where Help is Needed

- **Testing**: `tests/` covers the trade lifecycle, exits, the strategy pipeline and wallet discovery with a faked network. The live PumpPortal Lightning and Jupiter paths have never been tested against the real services; `status.py`, `monitor.py` and `telegram_ui.py` have no tests.
- **Backtesting**: A replay mode that processes historical data would help validate strategies.
- **New strategies**: The strategy interface is simple -- implement `evaluate()` and return a `Signal`.
- **Exchange support**: Currently pump.fun only. Raydium/Orca integration would expand coverage.
- **Documentation**: Strategy tuning guides, deployment walkthroughs, architecture deep-dives.

## Reporting Issues

- Include the relevant log output (from `bot.log`)
- Specify whether you are running in paper or live mode
- Include your Python version and OS

## Security

- Never commit `.env` files, private keys, or API secrets
- If you discover a security vulnerability, open a private issue or contact the maintainers directly
- Do not submit PRs that disable safety checks or risk limits without clear justification
