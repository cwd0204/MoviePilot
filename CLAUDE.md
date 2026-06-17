# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Authoritative Agent Guide

**Read [`AGENTS.md`](AGENTS.md) first.** It defines the full working contract for AI agents in this repo — layering rules (`endpoint → chain → module/helper/db`), where new capabilities go, comment/style conventions (Chinese by default), validation expectations, and commit conventions. Follow it. The notes below are supplemental: things that are not obvious from `AGENTS.md` or by reading the tree.

Today's date is provided in the user's CLAUDE.md (`# currentDate`); use it when interpreting "recent" or relative dates.

## Repository Shape

This repo is the **MoviePilot v2 backend + CLI + AI skills**. The frontend (Vue 3) lives in a separate repo (`MoviePilot-Frontend`); only its built `dist.zip` is pulled in at install time. Plugins and resources are also separate repos (`MoviePilot-Plugins`, `MoviePilot-Resources`). Don't try to find frontend source here.

Active branch is `v2`. Both default and main branches are `v2` — there is no `main`.

## Architecture in One Page

The runtime is a FastAPI app whose composition is non-obvious because most subsystems are loaded dynamically at startup:

- `app/main.py` boots uvicorn and `app/factory.py:create_app()` constructs the FastAPI instance with `lifespan` from `app/startup/lifecycle.py`.
- `app/startup/*_initializer.py` wires each subsystem during lifespan startup: `routers_initializer` (mounts `app/api/apiv1.py`), `modules_initializer` (loads pluggable backends from `app/modules/`), `plugins_initializer`, `scheduler_initializer`, `workflow_initializer`, `agent_initializer`, `command_initializer`, `monitor_initializer`. **If a feature isn't behaving in tests, check whether its initializer ran** — many things only work after lifespan startup.
- `app/api/apiv1.py` is the single registration point for HTTP routes. New endpoint files MUST be imported and `include_router`'d here, otherwise they are dead code.
- `app/chain/` is the orchestration layer shared by HTTP, CLI, scheduler, agents, and message handlers. Inside `chain`, prefer `run_module()` / `async_run_module()` over reaching into `ModuleManager` directly.
- `app/modules/` holds pluggable backends (downloaders, media servers, message channels, indexers, recognition, filters). Each follows the base-class contract from `AGENTS.md` §4. New code must not introduce `module → chain` or `module → module` coupling.
- `app/agent/` is the LLM agent runtime (LLM providers, tools, middleware, memory, prompts). `app/agent/runtime.py` is the entry point.
- `app/workflow/actions/` holds workflow action implementations (actions are user-composable steps).
- `app/db/` has SQLAlchemy models under `models/` plus `*_oper.py` data-access wrappers. Don't query models directly from endpoints — go through `*_oper` or `chain`.
- `app/core/config.py` holds `Settings` / `ConfigModel` (env-level config). **Runtime user config goes through `SystemConfigKey` in `app/schemas/types.py` + `SystemConfigOper`** — don't scatter raw string keys.
- The MCP/REST tool surface (used by AI agents and external integrations) is documented in [`docs/mcp-api.md`](docs/mcp-api.md). The endpoint lives at `app/api/endpoints/mcp.py`.

## Database Migrations

Migrations are Alembic-driven and live in `database/versions/`. Files are named `<rev>_<version>.py` (e.g., `b8f6e3a1c2d4_2_2_5.py`). When you change a model under `app/db/models/`, you MUST add a migration in the same change — `AGENTS.md` §8 enforces this. `database/env.py` reads `Base.metadata` from `app.db`. There is also a separate `app/modules/postgresql/` for Postgres-specific concerns; the default DB is SQLite.

## Common Commands

```bash
# Install / update dependencies (pip-tools is mandatory; do NOT hand-edit requirements.txt)
pip install -r requirements.txt
pip-compile requirements.in                       # regenerate lock after editing requirements.in
pip-compile --upgrade-package <pkg> requirements.in

# Run the backend locally (after env is set up via `moviepilot setup` or manual venv)
python app/main.py

# Local CLI (manages install, init, services, config, scheduler, tools, agent)
./moviepilot help
./moviepilot help <command>                       # nested help, e.g., `help config set`
./moviepilot config keys                          # list all SystemConfigKey entries with descriptions
./moviepilot tool list                            # list dynamic tools exposed to agent/MCP
./moviepilot scheduler list                       # list scheduled jobs
./moviepilot start | stop | restart | status | logs

# Tests — pytest is the primary runner
pytest                                            # full suite
pytest tests/test_metainfo.py                     # single file
pytest tests/test_metainfo.py::MetaInfoTest::test_metainfo   # single test
python tests/run.py                               # legacy unittest curated subset (recognition, scrape, subscribe)

# Lint — only error-level pylint findings block; warnings/conventions are disabled by .pylintrc
pylint app/

# Dependency security check (run after dependency changes)
safety check -r requirements.txt --policy-file=safety.policy.yml
```

The `moviepilot` shell script is the user-facing CLI; `app/cli.py` is the Python entrypoint it dispatches into. When you change CLI behavior, `AGENTS.md` §8 requires updating both the script's help text AND `docs/cli.md`.

## Skills

`skills/<name>/SKILL.md` files are AI-agent skills shipped with the repo (separate from Claude Code's own skills). They have YAML front matter and reference helper scripts via paths relative to the `SKILL.md`. When adding a skill, follow the existing structure — see `skills/moviepilot-cli/SKILL.md` as the canonical example.

## Things That Bite

- **Endpoint not found?** Confirm it was imported and included in `app/api/apiv1.py` — adding a file under `app/api/endpoints/` is not enough.
- **Module not loading?** `ModuleManager` discovers modules under `app/modules/` based on the base-class contract in `AGENTS.md` §4. Missing `init_module`/`get_type`/etc. silently excludes the module.
- **Config not picked up at runtime?** Long-lived objects need `CONFIG_WATCH` + `on_config_changed()` + `get_reload_name()` to react to config changes (see `AGENTS.md` §5).
- **`requirements.txt` looks hand-edited.** It isn't — it is generated by `pip-compile`. Re-run pip-compile rather than patching it.
- **CI uses Python 3.12** but minimum supported is 3.11. Don't use 3.12-only syntax.
- **Don't touch `config/`, `.moviepilot.env`, or `*.db`** in the worktree unless explicitly asked — those are local runtime state.
