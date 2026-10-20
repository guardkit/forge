# Manifest-qualified tags for release images

## Problem

The release builder currently writes two tags for each image: the commit of that image's build-context repository and the release version. A later release can legitimately keep a context commit while changing another pinned repository. The manifest hash, labels and resulting image then change, but the bare context-commit tag does not. The builder correctly refuses to move an existing tag. The tag choice must allow those two distinct releases to coexist without weakening that refusal.

## Contract

Add an explicit `--manifest-qualified-source-tag` option to `scripts/build-release-image.sh`. With the option, select these two tags for every image:

```text
<image-name>:<full context-repository commit>-<full manifest SHA-256>
<image-name>:<release version>
```

Without the option, retain the current bare context-commit tag and version tag, including the current receipt shape and default plan output. A full 40-character commit, hyphen and full 64-character manifest hash produce a 105-character tag component, below Docker's 128-character limit. The manifest hash must be the same digest already computed from the exact manifest bytes and stored in the receipt and `com.guardkit.release.manifest.sha256` label. No truncated digest or extra manifest field is needed.

This is a tag identity, not a source revision. Keep `org.opencontainers.image.revision`, the per-repository commit labels, `FORGE_GIT_SHA`, and the manifest's repository pins equal to the actual context/source commits. Keep release-version and manifest labels unchanged. The candidate release uses one final accepted Forge commit for both the builder checkout and Forge's pinned image context. The option changes no image payload, runtime behavior or repository-source selection.

Use one selection rule in the script for plan display, floating-tag checks, the all-image existing-tag preflight, build arguments, proof and sweep references, failure cleanup, the top-level and per-image receipt, and completion output. Preserve two tags per image and the current top-level coordinator receipt duplication. A real build must still refuse **any** selected tag that already exists before fetching or building. `--allow-existing-tag` keeps its existing explicit behavior but is outside this release path; the new option must neither imply it nor delete or move old tags. A failed run removes only tags it wrote, while pre-existing tags remain at their original image IDs.

The existing builder already has the necessary seams: `tags_of()` drives the plan and preflight; the build loop separately constructs `TAG_COMMIT` and `TAG_VERSION`. It passes those names to buildx and `RUN_TAGS`, sweeps through `TAG_COMMIT`, and writes both into `BUILT`; the later proof, receipt and completion loops read them as `itagcommit` and `itagversion`. These are the actual variable names in the pinned builder, even though `TAG_COMMIT` will hold a qualified tag in the opt-in mode. Make the first tag selected by one helper used in both locations, so the plan cannot promise a different tag from the one actually built. Document the option in usage/help and correct the unconditional prose that currently says every image receives a bare commit tag. Avoid a new tagging subsystem or manifest schema.

## Verification before dependent release work

Extend `tests/scripts/test_build_release_image.py` through the existing public script entry. Keep the existing default-mode assertions. The test must cover:

1. A local fake engine starts with bare commit tags mapped to old image IDs. Default mode refuses them before any fetch or build.
2. Qualified mode against a manifest with the same context commit and a different named-context pin succeeds, writes two selected tags per image, and leaves the old bare tags mapped to their original IDs. Top-level and per-image receipt tag arrays must equal the tags the fake engine actually holds, and the receipt's manifest hash must equal the suffix of the selected first tag.
3. If either a qualified first tag or a version tag already exists, qualified mode refuses for the whole image set before fetch or build. The old tag and its image ID remain untouched.
4. An image whose context is a different pinned repository uses *that repository's* commit before the manifest hash. The standard revision/source labels still name that repository and its commit.
5. A failure after tags are written, including a late receipt-write failure, removes only this run's qualified/version tags. The pre-existing bare tags keep their original IDs.

The fake engine and local Git fixtures must not contact a real Docker daemon or network. Tests should observe the public script's exit code, engine state, plan, and receipt rather than testing only a helper or matching source text. Preserve the existing tests of default plan, existing-tag refusal, sweeps and cleanup.

Run the **full affected release-builder test file through `dell-test`**. Set `FORGE_CHECKOUT` to the exact pinned Forge checkout and `FORGE_PYTHON` to an absolute path to an existing pinned Forge virtualenv interpreter; the clean checkout need not contain a `.venv` directory. Run the same commands at the pinned base before implementation and at the candidate after implementation; record both exit codes, failing test names and any difference. These are the exact checks, with xdist, the cache provider and inherited pytest options disabled:

```sh
: "${FORGE_CHECKOUT:?set FORGE_CHECKOUT to the exact Forge checkout}"
: "${FORGE_PYTHON:?set FORGE_PYTHON to the absolute pinned Forge virtualenv interpreter}"
case "$FORGE_PYTHON" in /*) ;; *) echo 'FORGE_PYTHON must be absolute' >&2; exit 2 ;; esac
cd "$FORGE_CHECKOUT"
dell-test bash -n scripts/build-release-image.sh
dell-test timeout 3600 env PYTHONPATH="$FORGE_CHECKOUT" "$FORGE_PYTHON" -m pytest -q -p no:xdist -p no:cacheprovider -o addopts= tests/scripts/test_build_release_image.py
```

The design review checks this contract and the existing callers before source implementation. The later implementation review checks the complete candidate, actual test results and the exact commit. A release build remains a separate acceptance gate: its finalized manifest and selected tags must pass a read-only plan, then the existing preflight, image proofs, sweep, receipt and old-tag-preservation checks. A source test result alone does not establish that an image was built or installed.
