# Changelog

## [1.0.0] - 2026-09-25

Initial release.

### Added

- Composite action that finds your pull requests to other people's repositories with the GitHub GraphQL search API, following the cursor 100 results at a time, and renders three self-contained animated SVG cards: a summary, the merged pull requests newest first, and the open ones grouped by repository.
- Repositories you own and private repositories are always left out. `exclude-owners` leaves out whole owners, such as your own organizations, and `exclude-repos` leaves out single repositories (`owner/repo`) or every repository of an owner (`owner/*`), case-insensitively, from both lists, the summary counts and the outputs.
- Inputs `username`, `token`, `exclude-owners`, `exclude-repos`, `output-summary`, `output-merged`, `output-review` (an empty string skips that card), `max-rows` and `accent`.
- Outputs `merged`, `open`, `projects` and `stars`.
- When the GitHub API fails, the existing cards are kept and the run exits 0. With no earlier card to keep, it exits 1.
- Pull request titles are HTML-escaped, en and em dashes become hyphens, and emoji are dropped.
- Installs Python 3.13 with `actions/setup-python@v7`.
- CI: ruff, pytest on Python 3.12, 3.13 and 3.14, and a job that runs `action.yml` from the checkout against the live API and checks that `exclude-repos` keeps the listed repositories off every card.
