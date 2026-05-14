# datasette-ip-rate-limit

[![PyPI](https://img.shields.io/pypi/v/datasette-ip-rate-limit.svg)](https://pypi.org/project/datasette-ip-rate-limit/)
[![Changelog](https://img.shields.io/github/v/release/datasette/datasette-ip-rate-limit?include_prereleases&label=changelog)](https://github.com/datasette/datasette-ip-rate-limit/releases)
[![Tests](https://github.com/datasette/datasette-ip-rate-limit/actions/workflows/test.yml/badge.svg)](https://github.com/datasette/datasette-ip-rate-limit/actions/workflows/test.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/datasette/datasette-ip-rate-limit/blob/main/LICENSE)

Rate limit Datasette requests by IP

## Installation

Install this plugin in the same environment as Datasette.
```bash
datasette install datasette-ip-rate-limit
```
## Usage

Usage instructions go here.

## Development

To set up this plugin locally, first checkout the code. You can confirm it is available like this:
```bash
cd datasette-ip-rate-limit
# Confirm the plugin is visible
uv run datasette plugins
```
To run the tests:
```bash
uv run pytest
```
