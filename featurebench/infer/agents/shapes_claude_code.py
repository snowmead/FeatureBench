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
  evidence: [EvidenceRef]?
```

## Lifecycle States

| State | Type | Meaning |
|-------|------|---------|
| proposed | progressive | Candidate under consideration |
| promoted | progressive | Intent accepted, realization may be incomplete |
| canonical | progressive | Authoritative, fully realized with evidence |

Transitions: proposed -> promoted -> canonical (or terminal states: rejected, abandoned, etc.)

## Realization & Evidence

Realization answers: "What artifacts IMPLEMENT this Shape?" (source files, configs, APIs)
Evidence answers: "What PROVES this Shape works?" (test files, benchmarks, reviews)

```yaml
RealizationBinding:
  uris: [string]              # file paths to source/implementation files
  role: primary | supporting | interface | verification | migration | docs

EvidenceRef:
  id: string                  # unique identifier for this evidence
  type: test_report | review | benchmark | attestation
  uris: [string]              # file paths to test files, CI reports, etc.
  trusted: boolean?            # whether this evidence source is trusted
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
8. Add evidence entries to shapes (test file mappings):
   - For each shape, find corresponding test files
   - Edit YAML to add: evidence: [{id: "tests-<name>", type: test_report, uris: [test file paths]}]
9. Run `shapes doctor` to validate

## YAML Conventions

- Use literal block scalars (| or >) for multi-line strings
- Dates are ISO 8601 ("2026-03-12")
- Versions are semver (1.0.0)
- IDs are integers (auto-assigned by CLI)
- Optional fields can be omitted entirely
"""


BOOTSTRAP_PROMPT = r"""You are analyzing a Python project to create a deep semantic map of its architecture, patterns, and invariants using the Shapes specification.

The `shapes` CLI is installed at /usr/local/bin/shapes. Read the skill instructions at /testbed/.shapes-skill.md and the spec reference at /testbed/.shapes-spec-reference.md.

Your goal is to create a shapes store that would allow a developer who has NEVER seen this codebase to understand:
- What each module/component does and WHY it exists
- How data flows through the system
- What patterns and invariants MUST be upheld when making changes
- Where the key function boundaries and interfaces are
- What coding conventions the project follows

## Process

1. **Deep exploration**: Read README, pyproject.toml/setup.py, directory structure, AND key source files in each major module. Read at least 2-3 core source files per module to understand patterns.

2. **Initialize**: Run `shapes init --name <project-name>`

3. **Create architecture shapes** (kind: system, service, module):
   - One system shape for the project root
   - Module shapes for each major package/subsystem
   - Edit YAML: fill intent.summary (WHY), goals, non_goals
   - Add realization bindings for SOURCE files only (not test files)
   - Set parent-child relationships

4. **Create component shapes** (kind: component, interface):
   - Key classes and abstractions within each module
   - Public API surfaces and their contracts
   - Critical internal components that enforce invariants
   - Edit YAML: include realization bindings to specific source files/classes

5. **Create pattern constraints** (kind: invariant, policy):
   - Error handling patterns (how does this project handle/propagate errors?)
   - Import conventions (lazy imports? conditional? star imports?)
   - Naming conventions (snake_case functions, CamelCase classes, prefixes?)
   - Testing patterns (fixtures, mocking approach, test organization)
   - Type annotation patterns (strict typing? optional? runtime checked?)

6. **Create data flow constraints** (kind: invariant):
   - Input validation patterns (where and how is input validated?)
   - State management (mutable vs immutable, thread safety patterns)
   - Serialization/deserialization contracts
   - Configuration loading patterns

7. **Create boundary constraints** (kind: requirement):
   - Module boundaries that must not be crossed (no circular imports, layer violations)
   - Public vs private API boundaries
   - Backward compatibility requirements

8. **Map test files to shapes via evidence**:
   - For each module/component shape, find its corresponding test files
   - Edit the shape YAML to add evidence entries pointing to test files:
     ```yaml
     evidence:
       - id: tests-<shape-name>
         type: test_report
         uris:
           - tests/test_module.py
           - tests/subdir/test_feature.py
     ```
   - This is critical — it allows developers to find which tests verify which modules
   - Test files go in `evidence` (proof the shape works), NOT in `realization` (implementation)

9. **Promote all entities**: `shapes promote <id> --reason "..."` with specific reasons

10. **Validate**: Run `shapes doctor`

## Guidelines
- Create 20-40 shapes and 10-20 constraints for thorough coverage
- Every shape MUST have realization bindings (source file paths) so the developer knows WHERE to look
- Every shape SHOULD have evidence entries mapping to its test files
- Every constraint MUST have a concrete rule description, not vague guidelines
- Focus on what a developer needs to know to make CORRECT changes, not just architectural overview
- Do NOT attempt to solve any bugs or add features — this is purely structural analysis
- CRITICAL: After completing all shapes and constraints, verify NO TODO placeholders remain:
  1. Run: `grep -rn "TODO" .shapes/shapes/ .shapes/constraints/`
  2. For EVERY file listed, read it and replace all TODO fields with actual content
  3. Repeat until `grep -rn "TODO" .shapes/` returns zero results
  Do NOT promote any entity that still has TODO fields.
  Run this verification one final time before `shapes doctor`.
"""


CLAUDE_MD_CONTENT = r"""# Project Architecture (Shapes)

This project's architecture, patterns, and constraints are mapped in `.shapes/`.
The shape hierarchy is in your system prompt. Use the `shapes` CLI for details.

## Finding Files

Shapes map both source files and test files:

**Source files** are in `realization` bindings:
```yaml
realization:
  - uris: [src/module/core.py, src/module/utils.py]
    role: primary
```

**Test files** are in `evidence` entries:
```yaml
evidence:
  - id: tests-module
    type: test_report
    uris: [tests/test_module.py, tests/test_utils.py]
```

```bash
shapes show <id>                    # See both realization AND evidence
shapes show <id> --format yaml      # Machine-readable for parsing paths
```

## Understanding Patterns

Constraints have a `rule` field describing the pattern you MUST follow:
```bash
shapes show <constraint-id>         # Read the rule field
```

## When You're Stuck

If you've edited a file multiple times without success:
1. Run `shapes show` for the shape that owns that file
2. Re-read its intent — are you solving the right problem?
3. Check its constraints — are you following the right patterns?
4. Check evidence URIs — are you testing the right files?

Do NOT modify `.shapes/` or CLAUDE.md.
"""


class ShapesClaudeCodeAgent(ClaudeCodeAgent):
    """Claude Code agent with shapes bootstrapping pre-step.

    Before the task-solving Claude Code session runs, this agent:
    1. Copies the pre-compiled shapes CLI binary into the container
    2. Writes the shapes spec reference and skill instructions
    3. Runs a separate Claude Code session to bootstrap .shapes/ with deep analysis
    4. Verifies shapes were created (agent queries them via CLI on demand)
    5. Writes a CLAUDE.md teaching the task agent the shapes CLI commands
    6. Amends the initial git commit to exclude .shapes/ from the patch
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shapes_context: str = ""

    @property
    def name(self) -> str:
        return "shapes_claude_code"

    def get_run_command(self, instruction: str) -> str:
        """Override to append shapes exploration instructions after the task.

        No shapes content is injected into the system prompt — the agent must
        discover the architecture by using the shapes CLI itself. Instructions
        are appended AFTER the task description (recency bias) and wrapped in
        <system_instructions> tags for higher adherence.
        """
        full_instruction = instruction.rstrip()
        allowed_tools = " ".join(self.ALLOWED_TOOLS)

        if self._shapes_context:
            shapes_appendix = (
                "\n\n<system_instructions>\n"
                "## Project Shapes (Architecture Map)\n\n"
                "This project has a `.shapes/` directory with its architecture, patterns, and "
                "constraints mapped as a DAG (directed acyclic graph). You MUST use the `shapes` "
                "CLI to explore it thoroughly before and during your work.\n\n"
                "### Understanding the Full DAG\n"
                "1. Run `shapes tree` to see the full shape hierarchy\n"
                "2. For each shape relevant to your task, run `shapes show <id>` to see:\n"
                "   - **parents**: what broader system/module this belongs to\n"
                "   - **children**: what sub-components it contains\n"
                "   - **constraints**: what invariants and patterns apply\n"
                "   - **realization**: which source files implement it\n"
                "   - **evidence**: which test files verify it (type: test_report)\n"
                "3. Traverse UP the DAG (parents) to understand broader context and system-level constraints\n"
                "4. Traverse DOWN the DAG (children) to understand sub-components and their boundaries\n"
                "5. Run `shapes show <constraint-id>` for each constraint to read its `rule` — "
                "these describe exactly how this project handles error handling, imports, naming, etc.\n"
                "6. Run `shapes list --kind constraint` to see ALL project-wide invariants\n\n"
                "### Finding Test Files\n"
                "Run `shapes show <id>` and check `evidence` entries — "
                "entries with `type: test_report` point to the test files for that module. "
                "Use these to find the RIGHT tests to run.\n\n"
                "### When You're Stuck\n"
                "If you've edited a file 3+ times without success, STOP. "
                "Run `shapes show` for that file's parent shape. Traverse up and down the DAG. "
                "Re-read intent, constraints, and realization bindings. "
                "Check if you're in the right module and following the right patterns.\n\n"
                "IMPORTANT: Do NOT modify any files in `.shapes/` or CLAUDE.md.\n"
                "</system_instructions>"
            )
            full_instruction = full_instruction + shapes_appendix

        escaped_instruction = shlex.quote(full_instruction)

        cmd = (
            f"NVM_DIR=${{NVM_DIR:-/opt/featurebench/nvm}}; "
            f'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" || true; '
            f"claude --verbose "
            f"-p {escaped_instruction} --allowedTools {allowed_tools} "
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
            "rm -rf /testbed/.shapes /testbed/CLAUDE.md && "
            "cd /testbed && git checkout -- .gitignore 2>/dev/null || true",
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
            # Create /agent-logs/ before bootstrap so tee doesn't fail
            self.cm.exec_command(container, "mkdir -p /agent-logs", log_file=log_file)

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

                # Log what shapes are available (agent will explore via CLI)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("\n" + "-" * 60 + "\n")
                    f.write("SHAPES AVAILABLE (agent will explore via CLI):\n")
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
        """Check if shapes were created and return a truthy string if so.

        No content is injected into the prompt — the agent discovers shapes
        by using the CLI itself. This just checks shapes exist (boolean flag
        for whether to include the shapes appendix instruction).
        """
        exit_code, output = self.cm.exec_command(
            container,
            "cd /testbed && /usr/local/bin/shapes list 2>/dev/null",
            log_file=log_file,
        )
        return output.strip() if output else ""

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
