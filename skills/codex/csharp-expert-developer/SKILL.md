---
name: csharp-expert-developer
description: Apply senior C#/.NET engineering guidance for backend work, including APIs, services, data access, DTOs, EF Core, SQL execution, tests, and builds. Use when implementing, refactoring, debugging, or reviewing C#/.NET code.
---

# C# Expert Developer

## Repo First

- Read available repo guidance such as `AGENTS.md`, `CONTEXT.md`, `docs/TDD/README.md`, and local pattern documents before code changes.
- Search existing code with `rg` before adding services, DTOs, controllers, helpers, SQL utilities, or tests.
- Prefer the existing project structure and dependency-injection patterns over new abstractions.
- Keep edits surgical and preserve unrelated files.
- Use current framework documentation when available; if unavailable, rely on local repo patterns and official docs only when needed.

## Backend Standards

- Keep controllers thin; put business logic in the appropriate service or domain layer already used by the target project.
- Do not return domain entities directly from controllers when the project uses DTOs/contracts.
- Handle nulls, authorization, validation, exceptions, and cancellation tokens deliberately.
- For SQL, use parameter binding. Never concatenate user values into SQL.
- Do not generate migrations unless explicitly requested.
- Add public API documentation only where the local project style expects it.

## Testing And Verification

- Follow the target project's test framework and naming conventions.
- Mock only external or unmanaged dependencies where practical; use real managed classes when cheap and stable.
- Run focused tests first, then relevant builds.
- If a build/test is blocked by local environment, report the exact command and blocker.

## Completion

- Do not claim done while relevant builds fail.
- If behavior changes are completed, update the target repo's feature/status documentation when such a rule exists.
