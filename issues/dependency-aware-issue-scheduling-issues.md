Label: ready-for-agent

# Dependency-Aware Issue Scheduling Issue Pack

## Target Product

Product: devloop-plan + devloop

## Parent PRD

`issues/dependency-aware-issue-scheduling.md`

## Execution order

1. [Issue 0011: Reject Invalid Issue Dependency Graphs](./0011-reject-invalid-issue-dependency-graphs.md)
2. [Issue 0012: Run Only Dependency-Ready Issues](./0012-run-only-dependency-ready-issues.md)
3. [Issue 0013: Resolve Root Blockers With Five Fair Passes](./0013-resolve-root-blockers-with-five-fair-passes.md)
4. [Issue 0014: Pause Runs on Global Backend Failures](./0014-pause-runs-on-global-backend-failures.md)
5. [Issue 0015: Validate Dependency-Aware Scheduling End to End](./0015-validate-dependency-aware-scheduling-end-to-end.md)

Dependencies are declared only in each issue's `## Blocked by` section. The
index provides deterministic priority among dependency-ready issues; it does
not create implicit dependencies.
