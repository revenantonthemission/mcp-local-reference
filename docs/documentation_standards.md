# Documentation Standards

<!-- SCOPE: Rules for writing and maintaining project documentation -->

## Structure Rules

| Rule | Description |
|------|-------------|
| SCOPE tag | Every document starts with an HTML comment defining its scope |
| Maintenance tag | Every document ends with a maintenance comment noting update triggers |
| Single Source of Truth | Each concept has exactly one canonical location; other docs link to it |
| No code blocks > 5 lines | Use tables, ASCII diagrams, or links to source files instead |

## Canonical Locations

| Concept | Canonical Document |
|---------|--------------------|
| What the server does | [requirements.md](project/requirements.md) |
| How it's built | [architecture.md](project/architecture.md) |
| Technology versions | [tech_stack.md](project/tech_stack.md) |
| Docker and CI config | [infrastructure.md](project/infrastructure.md) |
| Coding standards | [principles.md](principles.md) |
| Test strategy | `tests/README.md` |

## Update Triggers

| When this happens | Update these docs |
|-------------------|--------------------|
| New MCP tool added | requirements.md, architecture.md |
| Dependency version changed | tech_stack.md |
| New environment variable | infrastructure.md, README.md |
| New service class added | architecture.md |
| New test pattern | tests/README.md |
| Docker config changed | infrastructure.md |

## Format Priority

1. **Tables** — for parameters, configuration, comparisons
2. **ASCII/text diagrams** — for architecture and data flow
3. **Bullet lists** — for enumerations only
4. **Prose** — for context and rationale

---

<!-- Maintenance: Update when documentation structure changes -->
