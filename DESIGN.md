---
version: external
name: Frost
description: Editorial monospace design language used by Ratatoskr clients.
colors:
  ink: "#1C242C"
  ink-dark: "#E8ECF0"
  page: "#F0F2F5"
  page-dark: "#12161C"
  spark: "#DC3545"
rounded:
  none: 0px
---

# Frost design boundary

Frost is the design language used by Ratatoskr clients. Its editable tokens, components, responsive behavior, Storybook stories, and visual-regression tests are owned by the external `ratatoskr-web` and client-design sources, not this backend repository.

This file preserves the stable product-level constraints that affect backend-served UI artifacts. It is not a component inventory or a substitute for the client repository's current design documentation.

## Stable principles

1. **Ink, page, and one critical accent.** Light/dark themes invert ink and page. `spark` is reserved for critical state, not general hierarchy.
2. **Square, flat surfaces.** The system uses zero-radius containers, hairline borders, and no decorative shadows or gradients.
3. **Typography and alpha carry hierarchy.** Monospace UI type and an editorial serif reading face provide the primary contrast; critical color is not used as ordinary text emphasis.
4. **Accessible interaction.** Text contrast, keyboard focus, reduced motion, touch targets, and responsive layouts must be verified in the client that implements the component.

## Repository ownership

Ratatoskr serves the compiled web artifact from `app/static/web/` through FastAPI routes in `app/api/main.py`. Do not edit minified assets as if they were source. Make UI/design changes in the external frontend repository, run its current lint/type/test/build and visual checks, then refresh the compiled artifact through the release workflow.

Backend changes that affect clients must update and validate generated OpenAPI before client work begins. See [Web frontend integration](docs/reference/frontend-web.md) and [OpenAPI contract workflow](docs/reference/openapi-contract-workflow.md).

Last audited: 2026-07-15.
