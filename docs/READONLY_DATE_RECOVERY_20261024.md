# Read-only date and transport experiment - 24 October 2026

This change is confined to the independent test branch. It does not enable
Stage B, change main, publish an index, or access OneDrive.

## Observed baseline

At revision `0cdf133`, the fixed seven-document trial returned five correct
device/task/type/filename selections, one service error and one pending result.
The full twenty-document trial was not dispatched. These are development
documents, not unseen validation.

The remaining service error exhausted the existing twelve-failure allowance
before date confirmation. Date-panel traces in the pending document contained
inconsistent years, including readings of printed/signature ink. An OCR
disagreement is not evidence that the original paper contains a conflict.

## Changes

- A model with a recent unresolved transport failure gets one ordinary probe
  instead of two when another field is read. The half-open recovery stage,
  twelve-failure document cap, six-request reader cap, 45-second request timeout,
  server Retry-After and permitted primary/fallback models remain unchanged.
- `JOBSHEET_SIGNATURE_DATE_PARTS=1` enables narrower date-value panels and literal
  day/month/year group transcription. This switch requires dry-run mode and is
  set only in the read-only backtest workflow. The old reader remains unchanged
  without the switch.
- The crops retain bleed for tall date strokes. Stamps and overlapping ink are
  not erased. Digit groups cannot be filled from today, Asana or another panel.
  Partial/ambiguous output is unavailable, not corrected into a convenient date.
- Format retries remain one value from one physical panel, not extra votes.
  Two stable, distinct panels are still required for signature-date proof.
  Conflicts, identity checks, PM/CM project checks, candidate limits, live task
  uniqueness and the fourteen-day visit window are unchanged.

## Verification and gate

The first local check passed 606 synthetic tests plus syntax and dependency
checks. Date panels from all seventeen four-page source PDFs were rendered for
inspection; incomplete three-page sources were not treated as complete PMs.
The final crop margin is rechecked separately before commit.

Only one fixed-revision seven-case read-only trial is permitted first. All seven
must select the independently verified device, task, project type and filename
before dispatching all twenty. Source PDFs and the reviewed answer manifest are
fingerprinted and must not change. Previous failures are retained. Original
upload dates remain unknown; no execution-day or answer-date substitute is used.

Local tests and image inspection do not establish real-model improvement.
Record new requests, failed requests, tokens, time and unavailable billing cost
separately. Do not combine passes from different revisions into an apparent pass.
