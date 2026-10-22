# image-studio

fal.ai image generation, editing and clean-up skill for AI agents. Three modes: **create** from
scratch (brainstorm → prototype cheaply → produce the final), **edit/overlay** on an existing image,
and **clean up / enhance** an amateur photo into a professional shot.

## Structure

```
image-studio/
├── SKILL.md                    # Workflow doc: three modes, egress checks, model guidance
├── scripts/
│   ├── falgen.py               # CLI: generate, edit, upscale, removebg, costs
│   ├── moss_compile_prompt.py  # MOSS-Image layout compiler (vendored, Apache-2.0)
│   └── LICENSE-APACHE-2.0      # Upstream license for the vendored compiler
├── references/
│   ├── fal-models.md           # Model registry with verified pricing
│   ├── moss-layout.md          # In-image text/object placement dialect
│   └── cleanup-checklist.md    # Path C: common amateur-photo issues + fix phrasing
├── tests/
│   └── test_moss_compile_prompt.py  # Unit tests for the vendored compiler
└── examples/
    └── example-run.md          # Worked end-to-end example
```

## Requirements

- Python 3.8+
- `pip install fal-client requests`
- `FAL_KEY` environment variable (get one at https://fal.ai)
- Optional: `FAL_ADMIN_KEY` for live balance display

## License

MIT, except `scripts/moss_compile_prompt.py` (Apache-2.0, upstream OpenMOSS/MOSS-Image — its license ships in
`scripts/LICENSE-APACHE-2.0`); `references/moss-layout.md` is a derived work of upstream Apache-2.0 content.