# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it has releases.

PRs that change user-visible behavior, data schemas, label definitions or evaluation metrics should add a line under **Unreleased**.

## [Unreleased]

### Added

- Initial project documentation: README, agent and contributor guides, product requirements, roadmap, architecture, data, ML and development docs, ADRs, and GitHub issue/PR templates.
- Repository configuration: `.gitignore`, `.gitattributes`, `.editorconfig`, `.env.example`.

### Changed

- Align the product and staging with discovery/review/export for viewers, streamers and editors; keep model/application choices open and separate research outcomes from delivery.
- Repair proposed snapshots, unlabeled inference, source-time alignment and evaluation/label contracts under ADR-0009; document source-specific permissions and retention decisions.
- Establish shared planning, independent audit/verified repair and publication authorization guidance under ADR-0010.

### Fixed

- Synthetic-fixture exceptions no longer override private-key, environment or credential ignore rules.
