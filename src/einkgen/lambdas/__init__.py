"""AWS Lambda entrypoints."""

from __future__ import annotations

import os

# Refuse to import on a Lambda whose asset was produced by the CDK
# `EINKGEN_LOCAL_BUNDLE_SYNTH_ONLY=1` local-bundle path. That path stages a
# stub einkgen package with no dependencies and is meant for `cdk synth`
# only. If the env var ever leaks into a real `cdk deploy`, the resulting
# Lambda would crash with ModuleNotFoundError on import; failing here
# instead gives an actionable error message.
_SYNTH_ONLY_SENTINEL = "/var/task/SYNTH_ONLY_DO_NOT_DEPLOY"
if os.path.exists(_SYNTH_ONLY_SENTINEL):  # pragma: no cover - deploy guard
    raise RuntimeError(
        "einkgen Lambda asset is a synth-only stub. Re-deploy with Docker "
        "bundling (unset EINKGEN_LOCAL_BUNDLE_SYNTH_ONLY)."
    )
