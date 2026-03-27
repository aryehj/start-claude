# TL; DR
The author is a dilletente who starts a lot of projects, but is slow to learn syntax and commands. Therefore, the author automated most of the creation of safe-ish containerized workspaces for LLM-aided development in a given working directory. 

# start-claude.sh

Spins up an isolated [Apple Containers](https://developer.apple.com/documentation/virtualization)
dev environment for a project, pre-configured for Claude Code.

## Requirements

- macOS with Apple Containers installed (`container` CLI on PATH)
- Apple Silicon Mac
- Kata kernel installed: `container system kernel set --recommended`
- Rosetta 2 installed: `softwareupdate --install-rosetta --agree-to-license`

## Setup

```bash
# Optional: Make it available everywhere
echo 'export PATH="/Path/To/claude-setup:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

## Usage

```bash
# From inside your project directory
start-claude.sh

# Explicit project dir
start-claude.sh ~/projects/my-app

# Explicit project dir + container name
start-claude.sh ~/projects/my-app my-app
```

The script starts the container service automatically if it isn't already
running, so no manual `container system start` is needed beforehand.

On first run, the script pulls `debian:bookworm-slim`, installs tools inside a
temporary container, then exports it as `claude-dev:latest` — takes a few
minutes. Subsequent projects reuse the cached image and start instantly.

If you run the command again for a project that already has a container, it
re-attaches to the existing one rather than creating a new one.

## What's inside the container

| Tool | Notes |
|------|-------|
| `claude` | Claude Code CLI |
| `uv` / `uvx` | Python package manager |
| `node` / `npm` | Node.js LTS |
| `git` | Latest from apt |
| `ripgrep` | `rg` |
| `fd` | `fd-find` |
| `jq` | JSON CLI |
| `curl` / `wget` | |
| `python3` | System Python + pip |
| `build-essential` | gcc, make, etc. |

## Mounts

| Host | Container |
|------|-----------|
| Your project dir | Same path (e.g. `/Users/you/projects/my-app`) |
| `~/.claude` | `/root/.claude` |

`~/.claude` is shared across all containers — global memory, settings, and
sessions stay in sync with your host Claude Code installation.

**Authentication note:** Claude Code stores OAuth credentials in the macOS
Keychain, not in `~/.claude`. The bind-mount does not carry them into the
container, so `claude` will prompt you to log in the first time you use it in
a new container. Run `claude login` inside the container once; subsequent
re-attaches to the same container will already be authenticated.

## Rebuilding the image

To rebuild from scratch (e.g. after editing the setup script in
`start-claude.sh`), use the `--rebuild` flag:

```bash
start-claude.sh --rebuild
```

This removes the existing container for the project (if any) and the
`claude-dev:latest` image, then rebuilds from scratch.

## Environment variable reference

| Variable | Default | Description |
|----------|---------|-------------|
| `CLAUDE_CONTAINER_IMAGE` | `debian:bookworm-slim` | Override the base image |
