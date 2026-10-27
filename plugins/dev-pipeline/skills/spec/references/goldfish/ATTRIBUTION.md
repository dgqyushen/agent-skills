# Goldfish persona set — upstream attribution

These files are copied verbatim from the Elephant-Goldfish Model (EGM),
created at Google, Apache-2.0. "This is not an officially supported Google
product" applies to the upstream project.

- Upstream repository: https://github.com/google/elephant-goldfish-model
- Pinned upstream commit: `3d0b34821ce88ad328b0c9bf0e06d7eb5f1cd669` (main, 2026-10-24)
- Vendored on: 2026-10-25, into the dev-pipeline plugin (`spec` stage)
- `personas/` ← upstream `agents/*.md` (9 files)
- `standards/` ← upstream `resources/document_standards/*.md` (5 files)

Local edits policy: do not edit the vendored files. If an upstream update is
wanted, re-vendor the whole directory at a new pinned SHA and update this
note. Fixes for locally discovered problems go into
`../goldfish-spec-review.md` (the dispatch reference), not into the personas.

License: Apache-2.0; see the license field in each vendored file's
frontmatter. Full license text at the upstream repository
(`LICENSE` at the pinned commit, blob
`d645695673349e3947e8e5ae42332d0ac3164cd7`).