# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.

#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
"""
Stool Launcher - SLURM job submission for RepoProver.

Creates a run directory with:
  1. Snapshot of repoprover code
  2. Symlink to the formalization (Lean project) - uses original in place
  3. Run logs and recordings inside the original formalization

Usage:
    python -m repoprover.stool --name myrun --project /path/to/lean/project
    python -m repoprover.stool --name myrun --project . --pool_size 20
"""

import json
import os
import socket
import subprocess
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path

import simple_parsing

# ---- Cluster Detection ----


def get_cluster() -> str:
    """Detect cluster from hostname. Override for your infrastructure."""
    return "local"


def get_dump_root() -> str:
    """Get the root directory for dumps."""
    return f"/tmp/{getuser()}/repoprover"


# ---- Config Dataclass ----


@dataclass
class StoolArgs:
    """SLURM launcher for RepoProver CLI."""

    # Required: run name (used for dump directory and SLURM job name)
    name: str = ""

    # Required: path to Lean project with manifest.json
    project: str = "."

    # Output directory (defaults to /tmp/$USER/repoprover/$name)
    output: str = ""

    # CLI options
    pool_size: int = 10
    clean: bool = False
    verbose: bool = False
    prs_to_issues: bool = False  # Convert existing PRs to issues on startup
    agents_per_target: int = 1  # Max agents per theorem/issue (effective = min(this, 32 // n_targets))

    # Launcher settings
    launcher: str = "sbatch"  # sbatch | bash (if in salloc)
    dirs_exists_ok: bool = False
    override: bool = False

    # SLURM settings
    nodes: int = 1
    ncpu: int = 80
    mem: str = ""
    time: int = -1  # -1 = partition max
    partition: str = ""
    account: str = ""
    qos: str = ""
    constraint: str = ""
    exclude: str = ""

    anaconda: str = "default"


# ---- SBATCH Template ----

SBATCH_TEMPLATE = """#!/bin/bash

{exclude}
{qos}
{account}
{constraint}
#SBATCH --job-name={name}
#SBATCH --nodes={nodes}
#SBATCH --ntasks={tasks}
#SBATCH --cpus-per-task={ncpu}
#SBATCH --time={time}
#SBATCH --partition={partition}
#SBATCH --mem={mem}

#SBATCH --output={slurm_logs}/%j.stdout
#SBATCH --error={slurm_logs}/%j.stderr

#SBATCH --open-mode=append
#SBATCH --signal=USR2@120

eval "$({conda_exe} shell.bash hook)"
source activate {conda_env_path}

cd {code_dir}

export OMP_NUM_THREADS=1
export PYTHONPATH={code_dir}:$PYTHONPATH

{launch_command}
"""


# ---- Helpers ----


def copy_code(src_dir: str, dst_dir: str) -> None:
    """Copy repoprover code using rsync.

    Only copies allowlisted code directories (repoprover, shared libs).
    This avoids traversing large data dirs (leanenv ~80GB, fortdumps, etc).
    """
    print(f"📦 Copying code: {src_dir} -> {dst_dir}")

    # Allowlist of code directories to copy
    code_dirs = [
        "repoprover",
        "orchestrator",
        "services",
    ]

    for subdir in code_dirs:
        src_path = Path(src_dir) / subdir
        if src_path.exists():
            rsync_cmd = (
                f"rsync -arm --copy-links --include='**/' --include='*.py' --exclude='*' {src_path}/ {dst_dir}/{subdir}"
            )
            subprocess.call([rsync_cmd], shell=True)
            print(f"  ✓ {subdir}/")


def link_lean_project(src_dir: str, dst_link: str) -> None:
    """Create symlink to Lean project directory.

    Uses the original formalization in place (avoids slow copy of .lake/.git).
    """
    print(f"🔗 Linking Lean project: {dst_link} -> {src_dir}")
    os.symlink(src_dir, dst_link)


def get_partition_max_time(partition: str) -> int:
    """Get max time for partition in minutes."""
    try:
        sinfo = json.loads(subprocess.check_output("sinfo --json", shell=True))["sinfo"]
        for info in sinfo:
            if info["partition"]["name"] == partition:
                if info["partition"]["maximums"]["time"]["infinite"]:
                    return 14 * 24 * 60  # 14 days
                return info["partition"]["maximums"]["time"]["number"]
    except Exception:
        pass
    return 3 * 24 * 60  # Default 3 days


def validate_args(args: StoolArgs) -> None:
    """Validate and transform args."""
    if not args.name:
        raise ValueError("--name is required")

    # Resolve project path
    args.project = str(Path(args.project).resolve())
    if not Path(args.project).is_dir():
        raise ValueError(f"Project directory not found: {args.project}")

    # Set output directory if not specified
    if not args.output:
        args.output = str(Path(get_dump_root()) / args.name)

    if args.time == -1:
        args.time = get_partition_max_time(args.partition)
        print(f"Using partition max time: {args.time} minutes")

    # Transform optional SBATCH directives
    if args.constraint:
        args.constraint = f"#SBATCH --constraint={args.constraint}"
    if args.account:
        args.account = f"#SBATCH --account={args.account}"
    if args.qos:
        args.qos = f"#SBATCH --qos={args.qos}"
    if args.exclude:
        args.exclude = f"#SBATCH --exclude={args.exclude}"

    # Resolve anaconda path
    if args.anaconda == "default":
        args.anaconda = subprocess.check_output("which python", shell=True).decode().strip()

    args.mem = args.mem or "0"

    assert args.partition, "Partition required"
    assert args.nodes > 0, "Nodes must be > 0"


# ---- Main ----


def launch_job(args: StoolArgs) -> None:
    """Generate sbatch script and submit job.

    Directory structure created:
        {output}/
        ├── code/                    # Snapshot of repoprover Python code
        │   └── repoprover/
        ├── formalization/           # Snapshot of Lean project
        │   ├── *.lean
        │   ├── manifest.json
        │   └── runs/                # Created by CLI, logs go here
        │       └── <timestamp>/
        │           ├── logs/
        │           │   ├── debug.log
        │           │   ├── info.log
        │           │   └── error.log
        │           ├── session.jsonl
        │           └── agents/
        ├── slurm_logs/              # SLURM stdout/stderr
        ├── submit.slurm             # SLURM script
        └── launch_config.json       # Launch configuration
    """
    validate_args(args)

    dump_dir = Path(args.output)
    code_dir = dump_dir / "code"
    formalization_link = dump_dir / "formalization"  # Symlink to original project
    slurm_logs = dump_dir / "slurm_logs"

    print(f"\n{'=' * 60}")
    print("RepoProver Stool Launcher")
    print(f"{'=' * 60}")
    print(f"Run name:        {args.name}")
    print(f"Source project:  {args.project}")
    print(f"Dump directory:  {dump_dir}")
    print(f"{'=' * 60}\n")

    # Handle existing directory
    if dump_dir.exists():
        if args.override:
            import shutil

            confirm = input(f"Delete existing '{dump_dir}'? (yes/no): ")
            if confirm.lower() == "yes":
                shutil.rmtree(dump_dir)
                print(f"Deleted: {dump_dir}")
            else:
                print("Cancelled.")
                return
        elif not args.dirs_exists_ok:
            raise ValueError(
                f"Output directory already exists: {dump_dir}\nUse --dirs_exists_ok to reuse or --override to delete"
            )

    # Create directories
    dump_dir.mkdir(parents=True, exist_ok=True)
    code_dir.mkdir(parents=True, exist_ok=True)
    slurm_logs.mkdir(parents=True, exist_ok=True)

    # Copy repoprover code
    copy_code(os.getcwd(), str(code_dir))

    # Link to Lean project (avoid slow copy of .lake/.git)
    if not formalization_link.exists():
        link_lean_project(args.project, str(formalization_link))

    # Save launch config for reference
    config = {
        "name": args.name,
        "source_project": args.project,
        "output": args.output,
        "pool_size": args.pool_size,
        "clean": args.clean,
        "verbose": args.verbose,
    }
    with open(dump_dir / "launch_config.json", "w") as f:
        json.dump(config, f, indent=2)
    print(f"📄 Saved launch config: {dump_dir / 'launch_config.json'}")

    # Build launch command - use srun for multi-node execution
    # All nodes run the same command; workers auto-detect rank > 0 and switch to worker mode
    cmd_parts = [
        "srun --label python -u -m repoprover.cli run",
        f'"{formalization_link}"',
        f"--pool-size={args.pool_size}",
    ]
    if args.clean:
        cmd_parts.append("--clean")
    if args.verbose:
        cmd_parts.append("--verbose")
    if args.prs_to_issues:
        cmd_parts.append("--prs-to-issues")
    if args.agents_per_target > 1:
        cmd_parts.append(f"--agents-per-target={args.agents_per_target}")

    launch_command = " ".join(cmd_parts)

    # Generate sbatch
    conda_exe = os.environ.get("CONDA_EXE", "conda")
    conda_env_path = os.path.dirname(os.path.dirname(args.anaconda))

    sbatch = SBATCH_TEMPLATE.format(
        name=args.name,
        slurm_logs=slurm_logs,
        code_dir=code_dir,
        nodes=args.nodes,
        tasks=args.nodes,
        ncpu=args.ncpu,
        mem=args.mem,
        time=args.time,
        partition=args.partition,
        qos=args.qos,
        account=args.account,
        constraint=args.constraint,
        exclude=args.exclude,
        conda_exe=conda_exe,
        conda_env_path=conda_env_path,
        launch_command=launch_command,
    )

    # Write sbatch script
    script_path = dump_dir / "submit.slurm"
    with open(script_path, "w") as f:
        f.write(sbatch)
    print(f"📄 Wrote SLURM script: {script_path}")

    # Submit job
    print(f"\n🚀 Submitting with {args.launcher}...")
    exit_code = os.system(f"{args.launcher} {script_path}")

    if exit_code == 0:
        print(f"\n{'=' * 60}")
        print("✅ Job submitted successfully!")
        print("=" * 60)
        print(f"   Dump dir:      {dump_dir}")
        print(f"   Code:          {code_dir}")
        print(f"   Formalization: {formalization_link} -> {args.project}")
        print(f"   SLURM logs:    {slurm_logs}/")
        print(f"   Run logs:      {args.project}/runs/<timestamp>/logs/")
        print("\nMonitor with:")
        print(f"   tail -f {slurm_logs}/*.stdout")
        print("=" * 60)
    else:
        print(f"\n❌ Job submission failed with exit code: {exit_code}")


if __name__ == "__main__":
    args: StoolArgs = simple_parsing.parse(StoolArgs)
    launch_job(args)
