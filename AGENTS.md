# Pocket Manga Editor - Agent Guidance

## Ecosystem
This repository is part of `ddeka-projects`, a personal ecosystem of independent tools:
- `pocket-entertainment-catalog`: entertainment journeys and editing-work tracking.
- `pocket-manga-editor`: manga reading, page selection, and export.
- `pocket-media-clip-selector`: video-moment bookmarking and review.
- `pocket-media-manager`: local media recommendations, playback, and feedback.

They share a development philosophy, not a required architecture or runtime. Keep each repository independently maintainable.

## This application
The app supports reading manga, selecting pages, and exporting the chosen pages. Source images must remain untouched. Protect reading/editing positions, saved selections, and existing output; preserve transactional export and recovery guarantees.

## Working principles
- Build for one user: a Windows-hosted app accessed primarily from an iPhone over a trusted home LAN, with desktop browser support. Avoid unnecessary accounts, multi-user systems, or public services.
- Prioritize simplicity, reliability, maintainability, and data safety over extra features or abstraction.
- Treat brainstorming as discussion, not permission to edit. Evaluate whether a change is worthwhile and challenge assumptions constructively.
- Read this repository's current README, docs, and code before deciding implementation details. Verify relevant behavior and tests where feasible; report checks that need Windows or device testing.
- Look at sibling repositories for useful precedents when relevant and available, but do not modify them unless explicitly requested. Keep changes focused.
- Documentation is part of the change, not optional follow-up. When meaningful work changes behavior, limitations, setup, commands, scripts, or stored data, update the relevant README/docs in the same task unless the user explicitly restricts file changes. Explain what changed, why it matters, and how to use it; avoid unnecessary code detail.