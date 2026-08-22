# Result and experiment intelligence rules

Rules for developing result/experiment analysis features. Result analysis must
be grounded in retrieved evidence; prefer structured artifacts and recorded
metadata over free-form log interpretation.

- Preserve raw logs and artifacts; derived summaries must not replace source
  data.
- Identify every conclusion's job/run, artifact path, metric, or log evidence.
- Distinguish observed facts, calculated values, and LLM inference.
- Represent missing or unreadable outputs as unknown, not success or failure.
- Support comparison across runs by project, dataset/version, command/config,
  worker, code revision, timestamps, status, and recorded metrics.
- Make recommendations reviewable and require approval before rerun or
  mutation.
- Keep result collection failure separate from the job's execution status.
