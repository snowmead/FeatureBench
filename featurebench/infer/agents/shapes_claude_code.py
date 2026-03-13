"""
Shapes-augmented Claude Code agent implementation.

Extends ClaudeCodeAgent with a pre-run bootstrapping step that analyzes
the codebase and creates a .shapes/ directory with shapes and constraints,
giving the task-solving agent structural context about the project.
"""

import shlex
from pathlib import Path
from typing import TYPE_CHECKING

from docker.models.containers import Container

from featurebench.infer.agents.claude_code import ClaudeCodeAgent

if TYPE_CHECKING:
    from featurebench.infer.models import TaskInstance


# Shapes specification reference (embedded from spec-summary.md)
SPEC_SUMMARY = r"""# Shapes Specification — Condensed Reference

Version: 0.1.0 (Working Draft, March 2026)
Source: https://shapes.fyi/

## Shape Schema

```yaml
Shape:
  id: ShapeId                       # opaque identifier (int, string, UUID, etc.)
  name: string                      # human-readable name
  description: string               # what this Shape represents
  kind: string                      # system | service | feature | component | module | workflow | boundary | (custom)
  version: semver                   # e.g. "1.0.0", bumped on canonical amendments

  predecessors: [ShapeId]?          # lineage

  status:                           # tagged union — exactly one key:
    proposed:                       # | promoted: | canonical: | rejected: | abandoned: | reverted:
      date: iso8601
      reason: string?

  intent: Intent                    # see Intent Schema

  constraints:                      # mix of inline Constraint objects and ConstraintId references
    [Constraint | ConstraintId]?

  realization:                      # links to artifacts that embody this Shape
    [RealizationBinding]?

  evidence:                         # proof that the Shape is satisfied
    [EvidenceRef]?

  parents: [ShapeId]?               # inverse of children
  children: [Shape | ShapeId]?      # inline Shapes or ShapeId references (DAG)
```

## Intent Schema

```yaml
Intent:
  summary: string                   # REQUIRED — the why
  goals: [string]?
  non_goals: [string]?
```

## Constraint Schema

### Inline (embedded in a Shape)
```yaml
- id: ConstraintId
  kind: invariant | requirement | policy | limit
  rule: string
  enforcement: machine | human | hybrid
```

### Standalone (first-class node in `.shapes/constraints/`)
```yaml
Constraint:
  id: ConstraintId
  name: string
  description: string
  kind: invariant | requirement | policy | limit
  rule: string
  enforcement: machine | human | hybrid
  version: semver
  status: proposed | promoted | canonical | rejected | abandoned | reverted
  intent: Intent
  realization: [RealizationBinding]?
```

## Lifecycle States

| State | Type | Meaning |
|-------|------|---------|
| proposed | progressive | Candidate under consideration |
| promoted | progressive | Intent accepted, realization may be incomplete |
| canonical | progressive | Authoritative, fully realized with evidence |

Transitions: proposed -> promoted -> canonical (or terminal states: rejected, abandoned, etc.)

## Realization & Evidence

```yaml
RealizationBinding:
  uris: [string]              # file paths, URLs, other Shape IDs
  role: primary | supporting | interface | verification | migration | docs
```
"""


SKILL_INSTRUCTIONS = r"""# Shapes Manager — Bootstrap Instructions

The `shapes` CLI is installed at /usr/local/bin/shapes.

## Directory Layout

```
project-root/
└── .shapes/
    ├── manifest.yaml        # project-level metadata and ID counter
    ├── shapes/              # one YAML file per Shape
    ├── amendments/          # one YAML file per Amendment
    └── constraints/         # one YAML file per standalone Constraint
```

## CLI Commands

```bash
shapes init [--name <project-name>]                     # Initialize .shapes/
shapes new shape <name> --kind <kind> [--parent <id>]   # Scaffold Shape
shapes new constraint <name> --kind <kind>              # Scaffold Constraint
shapes promote <id> [--reason <text>]                   # Advance lifecycle
shapes list [--kind shape|amendment|constraint] [--status proposed|promoted|canonical]
shapes show <id>                                        # Full entity details
shapes tree [<id>]                                      # Hierarchy visualization
shapes doctor                                           # Validate store integrity
```

## Workflow

1. Run `shapes init` to create .shapes/
2. Create shapes with `shapes new shape <name> --kind <kind>`
3. Edit each generated YAML to fill in intent.summary, goals, non_goals, realization bindings
4. Promote with `shapes promote <id> --reason "intent mapped"`
5. Create constraints with `shapes new constraint <name> --kind <kind>`
6. Edit to set rule, enforcement, and intent
7. Promote constraints
8. Run `shapes doctor` to validate

## YAML Conventions

- Use literal block scalars (| or >) for multi-line strings
- Dates are ISO 8601 ("2026-03-12")
- Versions are semver (1.0.0)
- IDs are integers (auto-assigned by CLI)
- Optional fields can be omitted entirely
"""


BOOTSTRAP_PROMPT = r"""You are analyzing a Python project to create a semantic map of its architecture using the Shapes specification.

The `shapes` CLI is installed at /usr/local/bin/shapes. Read the skill instructions at /testbed/.shapes-skill.md and the spec reference at /testbed/.shapes-spec-reference.md.

Then:

1. Explore the codebase: README, directory structure, setup.py/pyproject.toml, key modules and packages
2. Run `shapes init` to create the .shapes/ directory
3. Create shapes for each major module/component using `shapes new shape <name> --kind <kind>`:
   - Edit each generated YAML to fill in:
     - intent.summary: WHY this module exists
     - intent.goals: what it accomplishes
     - intent.non_goals: what it deliberately excludes
     - realization: which source files implement it (use RealizationBinding format with uris and role)
   - Promote each shape: `shapes promote <id> --reason "intent mapped"`
4. Create constraints for cross-cutting invariants using `shapes new constraint <name> --kind <kind>`:
   - Edit each to set rule, enforcement mode (machine|human|hybrid), and intent
   - Reference constraint IDs from relevant shapes' constraints lists
   - Promote each constraint
5. Wire parent-child relationships between shapes by editing the YAML files directly

Focus on boundaries, contracts, and invariants — not trivial implementation details.
Do NOT attempt to solve any bugs or add features. This is purely structural analysis.
Keep it concise — 5-15 shapes, 3-7 constraints for a typical repo.
Run `shapes doctor` at the end to validate store integrity.
"""


CLAUDE_MD_CONTENT = r"""# Project Architecture (Shapes)

This project has been analyzed and mapped with shapes and constraints in `.shapes/`.

Before making changes, read the shapes to understand:
- Module boundaries and intent: `.shapes/shapes/*.yaml`
- Cross-cutting invariants and constraints: `.shapes/constraints/*.yaml`
- Which files implement which concerns (realization bindings in each shape)
- Parent-child relationships between modules

Start by running `shapes tree` for a hierarchy overview, then `shapes show <id>`
for the specific shapes relevant to the files you need to modify.
Use `shapes list --format yaml` for a machine-readable index.
"""


class ShapesClaudeCodeAgent(ClaudeCodeAgent):
    """Claude Code agent with shapes bootstrapping pre-step.

    Before the task-solving Claude Code session runs, this agent:
    1. Copies the pre-compiled shapes CLI binary into the container
    2. Writes the shapes spec reference and skill instructions
    3. Runs a separate Claude Code session to bootstrap .shapes/
    4. Captures shapes context (tree + YAML content) for injection into the task prompt
    5. Writes a CLAUDE.md instructing the task agent to use shapes
    6. Amends the initial git commit to include .shapes/ in the baseline
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shapes_context: str = ""

    @property
    def name(self) -> str:
        return "shapes_claude_code"

    def get_run_command(self, instruction: str) -> str:
        """Override to inject shapes context into both the user prompt and system prompt.

        The shapes context goes into --append-system-prompt for background reference,
        and a short acknowledgment directive is prepended to the -p user prompt to
        ensure the agent engages with it before starting work.
        """
        full_instruction = instruction.rstrip()
        allowed_tools = " ".join(self.ALLOWED_TOOLS)

        if self._shapes_context:
            # Prepend acknowledgment directive to the user prompt
            shapes_preamble = (
                "BEFORE you begin working on the task below, you MUST first:\n"
                "1. Read the architectural context in your system prompt (Project Architecture / Shapes)\n"
                "2. List the shapes and constraints you see\n"
                "3. Identify which shapes are relevant to this task and note their realization bindings\n"
                "4. Then proceed with the task\n\n"
                "IMPORTANT: Do NOT create, modify, or delete any files in the .shapes/ directory. "
                "Do NOT modify CLAUDE.md. These are read-only reference files.\n\n"
                "---\n\n"
            )
            full_instruction = shapes_preamble + full_instruction

        escaped_instruction = shlex.quote(full_instruction)

        cmd = (
            f"NVM_DIR=${{NVM_DIR:-/opt/featurebench/nvm}}; "
            f'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true; '
            f"claude --verbose "
            f"-p {escaped_instruction} --allowedTools {allowed_tools} "
        )

        if self._shapes_context:
            shapes_system_prompt = (
                "## Project Architecture (Shapes)\n\n"
                "This project has been analyzed and mapped with shapes and constraints. "
                "Use this structural context to understand module boundaries, intent, "
                "and invariants before making changes.\n\n"
                f"{self._shapes_context}"
            )
            escaped_system = shlex.quote(shapes_system_prompt)
            cmd += f"--append-system-prompt {escaped_system} "

        cmd += (
            f"--output-format stream-json "
            f"| tee /agent-logs/claude_code_stream_output.jsonl"
        )
        return cmd

    def post_run_hook(
        self,
        container: Container,
        log_file: Path,
    ) -> bool:
        """Remove .shapes/ and CLAUDE.md before patch extraction.

        The harness calls git add -A after the agent finishes. Removing these
        files from disk ensures they never appear in the patch diff.
        """
        self.cm.exec_command(
            container,
            "rm -rf /testbed/.shapes /testbed/CLAUDE.md",
            log_file=log_file,
        )
        return True

    def pre_run_setup(
        self,
        container: Container,
        instance: "TaskInstance",
        log_file: Path,
    ) -> bool:
        """Bootstrap .shapes/ before the task-solving agent runs."""
        self.logger.info(
            f"[shapes] Bootstrapping shapes for {instance.instance_id}..."
        )

        with open(log_file, "a", encoding="utf-8") as f:
            f.write("\n" + "=" * 60 + "\n")
            f.write(f"BEGIN Shapes Bootstrap: {instance.instance_id}\n")
            f.write("=" * 60 + "\n\n")

        try:
            # --- 1. Copy shapes binary into the container ---
            shapes_binary = self.env_vars.get(
                "SHAPES_BINARY_PATH",
                "/Users/snowmead/opt/shapes-cli/target/x86_64-unknown-linux-musl/release/shapes-cli",
            )
            shapes_path = Path(shapes_binary)
            if not shapes_path.exists():
                self.logger.error(
                    f"[shapes] Shapes binary not found at {shapes_binary}"
                )
                return False

            self.cm.copy_to_container(container, shapes_path, "/usr/local/bin/shapes-cli")
            exit_code, _ = self.cm.exec_command(
                container,
                "chmod +x /usr/local/bin/shapes-cli && ln -sf /usr/local/bin/shapes-cli /usr/local/bin/shapes",
                log_file=log_file,
            )
            if exit_code != 0:
                self.logger.error("[shapes] Failed to chmod shapes binary")
                return False

            # Verify the binary works
            exit_code, output = self.cm.exec_command(
                container, "/usr/local/bin/shapes --help", log_file=log_file
            )
            if exit_code != 0:
                self.logger.error(f"[shapes] shapes --help failed: {output}")
                return False

            self.logger.info("[shapes] shapes binary installed successfully")

            # --- 2. Write spec reference and skill instructions ---
            self._write_file_in_container(
                container,
                "/testbed/.shapes-spec-reference.md",
                SPEC_SUMMARY,
                log_file,
            )
            self._write_file_in_container(
                container,
                "/testbed/.shapes-skill.md",
                SKILL_INSTRUCTIONS,
                log_file,
            )

            # --- 3. Run Claude Code with bootstrap prompt ---
            bootstrap_timeout = int(
                self.env_vars.get("SHAPES_BOOTSTRAP_TIMEOUT", "1800")
            )
            escaped_prompt = shlex.quote(BOOTSTRAP_PROMPT.strip())

            bootstrap_cmd = (
                f"NVM_DIR=${{NVM_DIR:-/opt/featurebench/nvm}}; "
                f'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true; '
                f"source /installed-agent/setup-env.sh && cd /testbed && "
                f"claude --verbose "
                f"-p {escaped_prompt} "
                f"--allowedTools Bash Edit Write Read Glob Grep LS "
                f"--output-format stream-json "
                f"| tee /agent-logs/shapes_bootstrap_stream.jsonl"
            )

            self.logger.info(
                f"[shapes] Running bootstrap (timeout={bootstrap_timeout}s)..."
            )
            exit_code = self.cm.exec_command_stream(
                container,
                bootstrap_cmd,
                log_file=log_file,
                timeout=bootstrap_timeout,
            )

            if exit_code != 0:
                self.logger.warning(
                    f"[shapes] Bootstrap exited with code {exit_code} "
                    "(continuing — shapes may be partial)"
                )

            # --- 4. Verify .shapes/ was created ---
            exit_code, output = self.cm.exec_command(
                container,
                "ls /testbed/.shapes/manifest.yaml 2>/dev/null",
                log_file=log_file,
            )
            if exit_code != 0:
                self.logger.warning(
                    "[shapes] .shapes/manifest.yaml not found — bootstrap may have failed"
                )
                # Non-fatal: the task agent can still work without shapes
            else:
                # Show what was created
                exit_code, tree_output = self.cm.exec_command(
                    container,
                    "cd /testbed && /usr/local/bin/shapes list 2>/dev/null || true",
                    log_file=log_file,
                )
                self.logger.info(f"[shapes] Bootstrap results:\n{tree_output}")

                # --- 4b. Capture shapes context for injection into task prompt ---
                self._shapes_context = self._capture_shapes_context(
                    container, log_file
                )
                self.logger.info(
                    f"[shapes] Captured {len(self._shapes_context)} chars of shapes context"
                )

                # Log the full shapes context for verification
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("\n" + "-" * 60 + "\n")
                    f.write("SHAPES CONTEXT INJECTED VIA --append-system-prompt:\n")
                    f.write("-" * 60 + "\n")
                    f.write(self._shapes_context)
                    f.write("\n" + "-" * 60 + "\n\n")

            # --- 5. Write CLAUDE.md ---
            self._write_file_in_container(
                container,
                "/testbed/CLAUDE.md",
                CLAUDE_MD_CONTENT,
                log_file,
            )

            # --- 6. Clean up and exclude .shapes/ from git tracking ---
            # Delete bootstrap reference files
            self.cm.exec_command(
                container,
                "rm -f /testbed/.shapes-spec-reference.md /testbed/.shapes-skill.md",
                log_file=log_file,
            )
            # The bootstrap Claude Code session may have git-added .shapes/ files.
            # Remove them from the index (keep on disk), add to .gitignore, and
            # amend the initial commit so the task patch stays clean.
            gitignore_cmd = (
                "cd /testbed && "
                # Untrack .shapes/ and CLAUDE.md if they were added by bootstrap
                "git rm -r --cached .shapes/ CLAUDE.md .shapes-spec-reference.md .shapes-skill.md 2>/dev/null; "
                # Add to .gitignore so they stay untracked
                "echo '.shapes/' >> .gitignore && "
                "echo 'CLAUDE.md' >> .gitignore && "
                "git add .gitignore && "
                "git commit --amend --no-edit 2>/dev/null || true"
            )
            self.cm.exec_command(container, gitignore_cmd, log_file=log_file)

            self.logger.info("[shapes] Bootstrap complete")
            return True

        except Exception as e:
            self.logger.error(f"[shapes] Bootstrap failed: {e}")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"\nERROR (shapes bootstrap): {e}\n")
            # Non-fatal — let the task agent try without shapes
            return True

        finally:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write("\n" + "=" * 60 + "\n")
                f.write(f"END Shapes Bootstrap: {instance.instance_id}\n")
                f.write("=" * 60 + "\n\n")

    def _capture_shapes_context(
        self,
        container: Container,
        log_file: Path,
    ) -> str:
        """Read shapes tree and YAML content from the container.

        Returns a text block suitable for injection into the task prompt.
        """
        parts: list[str] = []

        # 1. shapes tree — hierarchy overview
        exit_code, tree_out = self.cm.exec_command(
            container,
            "cd /testbed && /usr/local/bin/shapes tree 2>/dev/null || true",
            log_file=log_file,
        )
        if tree_out and tree_out.strip():
            parts.append(f"### Shapes Hierarchy\n```\n{tree_out.strip()}\n```")

        # 2. Concatenate all shape YAML files
        exit_code, shapes_out = self.cm.exec_command(
            container,
            "cd /testbed && for f in .shapes/shapes/*.yaml; do "
            "[ -f \"$f\" ] && echo \"--- $f ---\" && cat \"$f\" && echo; "
            "done 2>/dev/null || true",
            log_file=log_file,
        )
        if shapes_out and shapes_out.strip():
            parts.append(f"### Shape Definitions\n```yaml\n{shapes_out.strip()}\n```")

        # 3. Concatenate all constraint YAML files
        exit_code, constraints_out = self.cm.exec_command(
            container,
            "cd /testbed && for f in .shapes/constraints/*.yaml; do "
            "[ -f \"$f\" ] && echo \"--- $f ---\" && cat \"$f\" && echo; "
            "done 2>/dev/null || true",
            log_file=log_file,
        )
        if constraints_out and constraints_out.strip():
            parts.append(
                f"### Constraint Definitions\n```yaml\n{constraints_out.strip()}\n```"
            )

        return "\n\n".join(parts)

    def _write_file_in_container(
        self,
        container: Container,
        path: str,
        content: str,
        log_file: Path,
    ) -> None:
        """Write a text file inside the container using heredoc."""
        # Use a unique delimiter unlikely to appear in content
        delimiter = "SHAPES_HEREDOC_EOF_7f3a"
        cmd = f"cat > {shlex.quote(path)} << '{delimiter}'\n{content}\n{delimiter}"
        exit_code, _ = self.cm.exec_command(container, cmd, log_file=log_file)
        if exit_code != 0:
            self.logger.warning(f"[shapes] Failed to write {path}")
