# Checkpoint override telemetry and threshold calibration reporting

## Description

Forge should record how often humans approve flagged outputs, reject auto-approved outputs, and override stage decisions so that checkpoint thresholds can be adjusted from evidence instead of intuition. The reporting should expose stage-by-stage calibration signals, including override rate, critical detection frequency, and review burden, because the documentation explicitly frames confidence-gated checkpoints as something that can mature over time rather than a one-off static configuration.

## Bounded Context

Checkpoint Management

## Source Documents

- forge-pipeline-orchestrator-refresh.md
- fleet-master-index.md
- big-picture-vision-and-durability.md

## Constraints

- Must remain advisory rather than automatically changing thresholds initially
- Must align with the confidence-gated checkpoint model already defined
- Must support per-stage and per-project analysis

## Dependencies

- FEAT-PO-001

## Suggested Context Files

- forge/docs/research/ideas/forge-pipeline-orchestrator-refresh.md
- forge/docs/research/ideas/fleet-master-index.md
- forge/docs/research/ideas/big-picture-vision-and-durability.md
