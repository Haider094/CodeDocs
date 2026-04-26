<h1 align="center">
  CodeDocs — Automatic Code Documentation, Powered by LLMs
</h1>

<p align="center">
  <em>Stop writing docs manually. Let AI do it for you.</em>
</p>

---

## Background

Keeping code documentation up to date is one of the most overlooked yet critical parts of software development. Most teams either skip it entirely or let it go stale.

**CodeDocs** solves this by using Large Language Models (LLMs) to automatically generate, maintain, and update documentation for your Python repositories — directly integrated into your Git workflow.

**Goal: Make accurate, readable documentation effortless for every developer.**

---

## Features

- **Auto-detects Git changes** — tracks file additions, deletions, and modifications automatically
- **AST-powered code analysis** — independently understands code structure to generate meaningful docs
- **Bidirectional call tracking** — identifies how objects reference each other for richer documentation
- **Seamless Markdown updates** — keeps your docs folder in sync with your code at all times
- **Multi-threaded generation** — concurrent processing for fast documentation at scale
- **Team-friendly** — integrates with `pre-commit` hooks for automated doc updates on every commit
- **Beautiful output** — generates structured Markdown docs, compatible with GitBook and similar tools

---

## Getting Started

### Installation

#### Using pip (Recommended)

```bash
pip install codedocs
```

#### Development Setup (PDM)

1. [Install PDM](https://pdm-project.org/latest/#installation)
2. Clone the repository:

```bash
git clone https://github.com/Haider094/CodeDocs.git
cd CodeDocs
```

3. Set up the virtual environment:

```bash
pdm venv create --name codedocs
pdm install
```

---

### Configuration

Set your OpenAI API key as an environment variable:

```sh
export OPENAI_API_KEY=YOUR_API_KEY          # Linux/Mac
set OPENAI_API_KEY=YOUR_API_KEY             # Windows (CMD)
$Env:OPENAI_API_KEY = "YOUR_API_KEY"        # Windows (PowerShell)
```

---

## Running CodeDocs

```sh
codedocs run                    # Generate or update documentation
codedocs run --print-hierarchy  # Also print the parsed repo structure
```

### Available Flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--model` | `-m` | `gpt-4o-mini` | LLM model to use |
| `--temperature` | `-t` | `0.2` | Generation temperature (lower = more deterministic) |
| `--request-timeout` | `-r` | `60` | API request timeout in seconds |
| `--base-url` | `-b` | `https://api.openai.com/v1` | Base URL for API calls |
| `--target-repo-path` | `-tp` | _(required)_ | Path to the repository to document |
| `--hierarchy-path` | `-hp` | `.project_doc_record` | Path for the project hierarchy file |
| `--markdown-docs-path` | `-mdp` | `markdown_docs` | Output folder for generated Markdown |
| `--ignore-list` | `-i` | _(none)_ | Comma-separated list of files/dirs to ignore |
| `--language` | `-l` | `English` | Language for generated documentation |
| `--log-level` | `-ll` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL) |

### Other Commands

```sh
codedocs clean   # Remove CodeDocs cache files
codedocs diff    # Preview which docs will be updated based on current code changes
```

---

## Pre-commit Hook Integration

Integrate CodeDocs into your Git workflow so docs update automatically on every commit.

**1. Ensure your repo is initialized:**

```sh
git init
```

**2. Install pre-commit:**

```sh
pip install pre-commit
```

**3. Create `.pre-commit-config.yaml` in your repo root:**

```yaml
repos:
  - repo: local
    hooks:
    - id: codedocs
      name: CodeDocs
      entry: codedocs
      language: system
      pass_filenames: false
      types: [python]
```

**4. Install the hook:**

```sh
pre-commit install
```

Now every `git commit` will automatically trigger CodeDocs to detect changes and update docs.

---

## Chat With Repo

CodeDocs includes an experimental **Chat With Repo** feature — an interactive Q&A interface for your codebase.

```sh
pip install codedocs[chat-with-repo]
codedocs chat-with-repo
```

---

## Roadmap

- [ ] Auto-generate `README.md` from project documentation
- [ ] Multi-language support (Java, C, C++, etc.)
- [x] Local model support (Llama, Qwen, GLM4, etc.)

---

## Contributing

Contributions are welcome! Feel free to open issues or submit pull requests.

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "Add my feature"`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request
