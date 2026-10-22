# Severity calibration

Assign severity by reasoning in this order, and keep the reasoning auditable in `impact` and
`trigger`:

1. **Consequence**: what happens when the defect triggers. Memory or data corruption, a security
   boundary crossed, a crash or hang of the system, an incorrect result, a resource leak, a
   performance loss, or a maintainability cost. This sets the starting level.
2. **Trigger path**: the concrete path that reaches it and the preconditions a caller, input or
   configuration must satisfy. If you cannot lay it out because it rests on an unverified
   assumption, mark the finding `speculative`.
3. **Reachability**: raise the level when untrusted, remote or unprivileged input reaches the
   defect. Never lower a level because you believe the path is unreachable or unlikely; if you
   cannot establish reachability, leave the level where the consequence put it.

A speculative finding is the one case where severity is capped, at medium, because the open
question is whether the defect is real. It is always reported, never dropped.

## Levels

- **critical**: memory corruption, data loss or corruption, a security vulnerability reachable by
  untrusted input, a kernel fault or hang reachable from userspace or the network, or breakage
  across the project (a public API or ABI broken without migration, a build broken for many
  boards). Ask: would it be better for the system to crash than keep running like this? Then it is
  critical.
- **high**: a clear functional defect in the changed behavior, a deadlock, a crash or hang on a
  realistic path, a leak that exhausts a resource over time, a broken public interface, or a
  build or boot failure for a supported configuration. Ask: can the system go down or become
  unusable with non-trivial probability? Then it is high.
- **medium**: a defect limited to some configurations, boards or error paths; a missing
  validation the API contract requires; inadequate error handling with a bounded consequence; a
  significant gap in tests for changed behavior; any speculative finding.
- **low**: a real but limited problem: a maintainability hazard, a misleading comment or
  documentation that contradicts the code, an edge case with minor consequence.
- **suggestion**: an optional improvement that is not required for correctness.

Use medium as the default and move from it only with a reason. Do not inflate: critical findings
must be critical, and a review that cries wolf is ignored. Do not deflate either: a real
high-severity defect labelled low because it "seems rare" is the worse mistake.

`request_changes` is warranted by any critical or high finding, or a medium finding a maintainer
would block on. Otherwise `comment` when there are findings and `approve` when there are none.
