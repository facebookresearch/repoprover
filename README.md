# RepoProver

[![Paper PDF](https://img.shields.io/badge/Paper-PDF-red?style=for-the-badge)](auto_textbook_formalization.pdf)
[![Formalization](https://img.shields.io/badge/Formalization-GitHub-green?style=for-the-badge)](https://github.com/facebookresearch/algebraic-combinatorics)
[![Blueprint Website](https://img.shields.io/badge/Blueprint-Website-blue?style=for-the-badge)](https://faabian.github.io/algebraic-combinatorics/)
[![Blueprint PDF](https://img.shields.io/badge/Blueprint-PDF-red?style=for-the-badge)](https://github.com/facebookresearch/algebraic-combinatorics/blob/main/blueprint/print/print.pdf)

Code for [Automatic Textbook Formalization (Gloeckle, Rammal, Arnal, Munos, Cabannes, Synnaeve, Hayat, 2026)](auto_textbook_formalization.pdf).

RepoProver is a multi-agent scaffold for large-scale formalization of mathematics textbooks in Lean. It orchestrates multiple LLM agents that collaborate on a shared git repository with the Lean project: sketcher agents translate definitions and theorem statements, prover agents fill in proofs, and reviewer agents enforce quality via pull request reviews. Coordination happens through a lightweight file-system-based issue tracker and a merge queue that ensures the main branch always builds.

This code produced an [automatic formalization](https://github.com/facebookresearch/algebraic-combinatorics) of the graduate textbook [Algebraic Combinatorics](https://arxiv.org/abs/2506.00738) by Darij Grinberg.

## Setup

Requires Python 3.10+. Install in editable mode:

```bash
pip install -e .
```

## Preparing a formalization project

RepoProver operates on a Lean project repository. Before running, you need to set up:

1. **Create a Lean project with Mathlib** and build it:
   ```bash
   lake init MyProject math
   lake update
   lake build
   ```

2. **Add LaTeX source files** under a `tex/` directory inside the project, organized by topic:
   ```
   MyProject/
   ├── lakefile.lean
   ├── lean-toolchain
   ├── lake-manifest.json
   ├── MyProject.lean           # root import file
   ├── MyProject/
   │   └── tex/                 # LaTeX source chapters
   │       ├── all.tex          # full textbook source (optional)
   │       ├── Topic1/
   │       │   ├── Chapter1.tex
   │       │   └── Chapter2.tex
   │       └── Topic2/
   │           └── ...
   ├── manifest.json            # chapter manifest (see below)
   ├── CONTENTS.md              # structure documentation (see below)
   └── issues/                  # issue tracker (see below)
   ```

   The tex files should be split by chapter/section so each can be assigned to a sketcher agent independently. An `all.tex` with the full source can be included for reference. Note that tex files are read-only — agents can read them but never modify source material.

3. **Create a `CONTENTS.md`** at the project root documenting the structure of tex sources and corresponding Lean files. The coordinator generates an initial version from the manifest, and agents update it as the Lean codebase evolves. It serves as the central reference for project structure, proof status and architecture notes.

4. **Create a `manifest.json`** at the project root listing the chapters to formalize and their target theorems/definitions. Each chapter entry has:
   - `id`: unique identifier for the chapter
   - `title`: human-readable chapter title
   - `source_path`: path to the LaTeX source file (relative to project root)
   - `target_theorems`: list of theorem/definition IDs to formalize from this chapter

   See [`configs/example_manifest.json`](configs/example_manifest.json) for a full example from the algebraic combinatorics case study.

5. **Create an empty `issues/` directory** at the project root. Agents use this as a lightweight file-system-based issue tracker — they create short YAML files here to flag blockers, request refactorings, or coordinate work across chapters.

6. **Initialize git** (with branch name `main`) in the project if not already done — RepoProver uses git for version control, branching and merging.

## Usage

### Running the coordinator

```bash
python -m repoprover run /path/to/lean/project --pool-size 10
```

This starts the main coordinator loop which launches sketcher, prover, maintainer and reviewer agents, manages the merge queue and tracks progress. The project state is saved in `.repoprover/` inside the Lean project directory.

Use `--clean` to start from scratch, `--verbose` for debug logging.

### Multi-node (SLURM)

For distributed runs across multiple machines, use the stool launcher:

```bash
python -m repoprover.stool --name myrun --project /path/to/lean/project
```

The stool launcher snapshots the repoprover code to a dump directory, symlinks the Lean project (avoiding slow copies of `.lake/` and `.git/`) and submits a SLURM job. Rank 0 runs the coordinator in a background thread; all ranks (including rank 0) run as workers that pull tasks from the coordinator.

Options:

- `--launcher bash` — run directly if already inside an `salloc` session
- `--pool-size N` — number of Lean REPL instances per node (default: 10)
- `--nodes N` — number of SLURM nodes (default: 1)
- `--agents-per-target N` — max parallel agents per theorem/issue (default: 1)
- `--prs-to-issues` — convert pending PRs to issues when resuming a run
- `--clean` — wipe state and restart from scratch
- `--dirs-exists-ok` — reuse an existing dump directory

See [`configs/example.yaml`](configs/example.yaml) for an example configuration.

### Analysis scripts

```bash
# Token usage breakdown by agent type and outcome
python scripts/count_tokens.py /path/to/lean/project

# Agent efficiency plots over time
python scripts/plot_agent_efficiency.py /path/to/lean/project --out ./plots
```

### Smoke test

A toy project is included under `examples/toy_project/` for quick testing. The setup script copies the files to a working directory, initializes git, fetches Mathlib and builds the project:

```bash
bash examples/toy_project/setup.sh /tmp/repoprover-toy-test
```

Then run repoprover on it:

```bash
source .venv/bin/activate
python -m repoprover run /tmp/repoprover-toy-test --pool-size 2 --verbose
```

The toy project has one chapter with 4 trivial targets (a definition and 3 theorems about doubling natural numbers).

### Trajectory viewer

To inspect agent trajectories from a run:

```bash
python -m repoprover.viewer --dir /path/to/lean/project/runs --port 8080
```

## License

This project is licensed under the terms in [LICENSE](LICENSE).
