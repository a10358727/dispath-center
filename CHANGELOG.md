# Changelog

All notable changes to Dispatch Center will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and published releases will use Semantic Versioning. Until the first published
release, compatibility and migration notes in this repository remain the
authoritative deployment guidance.

## [Unreleased]

### Added

- Repository contribution, security, review-ownership, and pull-request
  governance documents.
- Local lint, type-check, and coverage tooling definitions for CI and developer
  use.
- Independent Control Plane and dependency-free Node Agent wheel builds, with
  console entry points and CI installation smoke tests.
- Immutable bounded-context settings composed from the existing `AppConfig`,
  secret-safe startup reporting, and reviewed feature-flag lifecycle metadata.

### Changed

- Core runtime, optional LLM/MCP, test, and development dependencies are now
  separated. Existing Python module launchers remain supported.
- `NODE_AGENT_V1_ENABLED` remains behavior-compatible but now emits a
  deprecation warning in favor of the protocol/drain and assignment controls.

### Security

- Security-sensitive paths now have explicit review ownership metadata.
- Shared, OIDC, SMTP, Anthropic, and vLLM credentials are masked from both
  legacy and typed settings representations and omitted from startup logs.

## Release metadata requirements

Each future release entry must include:

- control-plane and node-agent versions;
- supported API and node-protocol versions;
- database schema version and migration/rollback notes;
- feature-flag and deprecation changes;
- checksums or SBOM location for release artifacts;
- deployment and canary evidence, where required.

[Unreleased]: https://github.com/a10358727/dispath-center/compare/HEAD
