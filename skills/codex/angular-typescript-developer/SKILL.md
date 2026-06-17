---
name: angular-typescript-developer
description: Apply senior Angular/TypeScript guidance for frontend work, including components, services, DTOs, state flow, tests, and builds. Use when implementing, refactoring, debugging, or reviewing Angular, TypeScript, HTML, SCSS, RxJS, forms, or frontend behavior.
---

# Angular TypeScript Developer

## Repo First

- Read available repo guidance such as `AGENTS.md`, `CONTEXT.md`, frontend pattern docs, completion checklists, and TDD docs before web changes.
- Search existing components, services, DTOs, and UI patterns with `rg` before adding code.
- Use current Angular/TypeScript/library documentation when available; if unavailable, rely on local patterns and official docs only when needed.

## Frontend Standards

- Use strong TypeScript types; avoid `any` unless existing API boundaries force it and explain why.
- Keep templates simple; move behavior into component/service methods.
- Avoid nested subscriptions. Prefer observable/signal patterns already used by the target project.
- Reuse existing services, DTO files, notification patterns, and UI helpers.
- Preserve existing UI conventions.
- Keep changes surgical and do not refactor unrelated components.

## Testing And Verification

- Add focused specs for changed behavior where practical.
- Run the target project's TypeScript compile, focused specs, and build commands for frontend changes.
- If browser verification is relevant and possible, run the app and inspect the actual UI.

## Completion

- Do not claim done while relevant TypeScript/build checks fail.
- If behavior changes are completed, update the target repo's feature/status documentation when such a rule exists.
